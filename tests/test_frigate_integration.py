"""Tests for the Frigate persistent-endpoint wiring: switches, sensors, and the
coordinator helpers (`_frigate_config`, `frigate_endpoint_url`,
`async_sync_frigate_endpoint`, `_frigate_resolve_inner`, …).

Pins the contracts so a refactor can't:
  - enable the High/Low switches by default (entity-spam),
  - leak a URL while the global feature flag or the per-cam switch is off,
  - publish a non-LOCAL session URL (credential-free injection is LOCAL-only),
  - forget to start/stop the front-door on toggle.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.bosch_shc_camera as bosch_init
from custom_components.bosch_shc_camera.frigate_endpoint import (
    FrontDoorConfig,
    InnerTarget,
)

CAM_ID = "11111111-1111-1111-1111-111111111111"
Coordinator = bosch_init.BoschCameraCoordinator


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ─────────────────────────────────────────────────────────────────────────────
# Coordinator helper double — binds the real frigate methods onto a light object.
# ─────────────────────────────────────────────────────────────────────────────


class _CoordDouble:
    _frigate_config = Coordinator._frigate_config
    _frigate_url_host = Coordinator._frigate_url_host
    _frigate_resolve_inner = Coordinator._frigate_resolve_inner
    _frigate_wanted = Coordinator._frigate_wanted
    async_sync_frigate_endpoint = Coordinator.async_sync_frigate_endpoint
    frigate_endpoint_url = Coordinator.frigate_endpoint_url
    async_stop_frigate_endpoints = Coordinator.async_stop_frigate_endpoints


def _make_coord(**options: object) -> _CoordDouble:
    opts = {
        "frigate_endpoints_enabled": False,
        "frigate_bind_host": "127.0.0.1",
        "frigate_bind_port": 0,
        "frigate_ip_allowlist": "",
        "frigate_auth_mode": "none",
        "frigate_token": "",
        "frigate_basic_user": "frigate",
        "frigate_idle_timeout": 60,
    }
    opts.update(options)
    c = _CoordDouble()
    c.options = opts  # type: ignore[attr-defined]
    c.data = {CAM_ID: {}}  # type: ignore[attr-defined]
    c._frigate_high_enabled = {}  # type: ignore[attr-defined]
    c._frigate_low_enabled = {}  # type: ignore[attr-defined]
    c._frigate_sticky_port = {}  # type: ignore[attr-defined]
    c._frigate_runner = None  # type: ignore[attr-defined]
    c._live_connections = {}  # type: ignore[attr-defined]
    c._tls_proxy_ports = {}  # type: ignore[attr-defined]
    c.async_update_listeners = MagicMock()  # type: ignore[attr-defined]
    c.hass = SimpleNamespace(loop=MagicMock())  # type: ignore[attr-defined]
    c.get_model_config = lambda _cam: SimpleNamespace(max_session_duration=3600)  # type: ignore[attr-defined]
    return c


# ── _frigate_config ──────────────────────────────────────────────────────────


def test_frigate_config_parses_options() -> None:
    c = _make_coord(
        frigate_bind_host="0.0.0.0",
        frigate_ip_allowlist=" 192.168.1.5 , 10.0.0.0/8 ,",
        frigate_auth_mode="basic",
        frigate_token="secret",
        frigate_basic_user="rec",
        frigate_idle_timeout=120,
        frigate_max_connections=16,
    )
    cfg = c._frigate_config()
    assert isinstance(cfg, FrontDoorConfig)
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.ip_allowlist == frozenset({"192.168.1.5", "10.0.0.0/8"})
    assert cfg.auth_mode == "basic"
    assert cfg.token == "secret"
    assert cfg.basic_user == "rec"
    assert cfg.idle_timeout == 120.0
    assert cfg.max_connections == 16


# ── _frigate_url_host ────────────────────────────────────────────────────────


def test_url_host_localhost() -> None:
    c = _make_coord()
    assert c._frigate_url_host("127.0.0.1") == "127.0.0.1"


def test_url_host_specific_ip_used_verbatim() -> None:
    # A concrete interface IP is routable as-is — no LAN detection.
    c = _make_coord()
    assert c._frigate_url_host("192.168.1.50") == "192.168.1.50"


def test_url_host_lan_detects_ip(monkeypatch) -> None:
    c = _make_coord()

    class _FakeSock:
        family = 2

        def connect(self, _addr: object) -> None:
            return None

        def getsockname(self) -> tuple[str, int]:
            return ("192.168.1.50", 0)

        def close(self) -> None:
            return None

    monkeypatch.setattr(bosch_init.socket, "socket", lambda *a, **k: _FakeSock())
    assert c._frigate_url_host("0.0.0.0") == "192.168.1.50"


def test_url_host_lan_fallback_on_oserror(monkeypatch) -> None:
    c = _make_coord()

    class _FailSock:
        def connect(self, _addr: object) -> None:
            raise OSError("no route")

        def close(self) -> None:
            return None

    monkeypatch.setattr(bosch_init.socket, "socket", lambda *a, **k: _FailSock())
    assert c._frigate_url_host("0.0.0.0") == "127.0.0.1"


# ── _frigate_wanted / frigate_endpoint_url gating ────────────────────────────


def test_wanted_requires_feature_and_switch() -> None:
    c = _make_coord(frigate_endpoints_enabled=False)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    assert c._frigate_wanted(CAM_ID) is False  # feature off
    c.options["frigate_endpoints_enabled"] = True  # type: ignore[index]
    assert c._frigate_wanted(CAM_ID) is True
    c._frigate_high_enabled[CAM_ID] = False  # type: ignore[attr-defined]
    assert c._frigate_wanted(CAM_ID) is False  # no switch on


def test_endpoint_url_none_when_feature_off() -> None:
    c = _make_coord(frigate_endpoints_enabled=False)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    assert c.frigate_endpoint_url(CAM_ID, "high") is None


def test_endpoint_url_none_when_switch_off() -> None:
    c = _make_coord(frigate_endpoints_enabled=True)
    assert c.frigate_endpoint_url(CAM_ID, "high") is None
    assert c.frigate_endpoint_url(CAM_ID, "low") is None


def test_endpoint_url_none_when_no_runner() -> None:
    c = _make_coord(frigate_endpoints_enabled=True)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    assert c.frigate_endpoint_url(CAM_ID, "high") is None  # runner None


def test_endpoint_url_built_when_running() -> None:
    c = _make_coord(frigate_endpoints_enabled=True)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    c._frigate_low_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    fake_runner = SimpleNamespace(
        has_server=lambda _c: True,
        port=lambda _c: 8600,
    )
    c._frigate_runner = fake_runner  # type: ignore[attr-defined]
    high = c.frigate_endpoint_url(CAM_ID, "high")
    low = c.frigate_endpoint_url(CAM_ID, "low")
    assert (
        high
        == "rtsp://127.0.0.1:8600/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=3600"
    )
    assert "inst=2" in low and "maxSessionDuration=3600" in low


def test_endpoint_url_none_when_port_zero() -> None:
    c = _make_coord(frigate_endpoints_enabled=True)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    c._frigate_runner = SimpleNamespace(  # type: ignore[attr-defined]
        has_server=lambda _c: True, port=lambda _c: 0
    )
    assert c.frigate_endpoint_url(CAM_ID, "high") is None


# ── async_sync_frigate_endpoint ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_stops_when_not_wanted() -> None:
    c = _make_coord(frigate_endpoints_enabled=False)
    runner = MagicMock()
    runner.has_server.return_value = True
    c._frigate_runner = runner  # type: ignore[attr-defined]
    await c.async_sync_frigate_endpoint(CAM_ID)
    runner.stop_server.assert_called_once_with(CAM_ID)


@pytest.mark.asyncio
async def test_sync_starts_when_wanted() -> None:
    c = _make_coord(frigate_endpoints_enabled=True)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    runner = MagicMock()
    runner.start_server = AsyncMock(return_value=8765)
    c._frigate_runner = runner  # type: ignore[attr-defined]
    await c.async_sync_frigate_endpoint(CAM_ID)
    runner.start_server.assert_called_once()
    assert c._frigate_sticky_port[CAM_ID] == 8765  # type: ignore[attr-defined]
    c.async_update_listeners.assert_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sync_creates_real_runner_when_none(socket_enabled) -> None:
    """When wanted and no runner exists yet, a real FrontDoorRunner is created
    and binds a port. Covers the lazy-create path."""
    import asyncio

    c = _make_coord(frigate_endpoints_enabled=True)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    c.hass = SimpleNamespace(loop=asyncio.get_event_loop())  # type: ignore[attr-defined]
    try:
        await c.async_sync_frigate_endpoint(CAM_ID)
        assert c._frigate_runner is not None  # type: ignore[attr-defined]
        assert c._frigate_runner.has_server(CAM_ID)  # type: ignore[attr-defined]
        assert c._frigate_sticky_port[CAM_ID] > 0  # type: ignore[attr-defined]
    finally:
        c.async_stop_frigate_endpoints()


@pytest.mark.asyncio
async def test_stop_endpoints_swallows_stop_all_error() -> None:
    c = _make_coord()
    runner = MagicMock()
    runner.stop_all.side_effect = RuntimeError("boom")
    c._frigate_runner = runner  # type: ignore[attr-defined]
    c.async_stop_frigate_endpoints()  # must not raise
    assert c._frigate_runner is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sync_retries_ephemeral_on_bind_error() -> None:
    c = _make_coord(frigate_endpoints_enabled=True)
    c._frigate_low_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    c._frigate_sticky_port[CAM_ID] = 9999  # type: ignore[attr-defined]
    runner = MagicMock()
    runner.start_server = AsyncMock(side_effect=[OSError("port taken"), 7000])
    c._frigate_runner = runner  # type: ignore[attr-defined]
    await c.async_sync_frigate_endpoint(CAM_ID)
    assert runner.start_server.call_count == 2
    assert c._frigate_sticky_port[CAM_ID] == 7000  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sync_ephemeral_retry_bind_error_does_not_raise() -> None:
    """Regression (bug-hunt 2026-07-03): the first OSError assumes "sticky
    port taken" and retries on an ephemeral port — but an ephemeral (port=0)
    bind still uses frigate_bind_host, so if THAT is unbindable (bad
    interface/IPv6 literal/etc.) the retry fails with the same OSError,
    previously uncaught. async_added_to_hass calls this on every HA restart
    for a RestoreEntity-restored "on" switch, so a bad frigate_bind_host used
    to break entity setup with a traceback on every restart instead of a
    clear log line."""
    c = _make_coord(frigate_endpoints_enabled=True)
    c._frigate_low_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    c._frigate_sticky_port[CAM_ID] = 9999  # type: ignore[attr-defined]
    runner = MagicMock()
    runner.start_server = AsyncMock(
        side_effect=[OSError("port taken"), OSError("cannot assign requested address")]
    )
    c._frigate_runner = runner  # type: ignore[attr-defined]
    # Must not raise — this is the regression itself.
    await c.async_sync_frigate_endpoint(CAM_ID)
    assert runner.start_server.call_count == 2
    # Sticky port must not be set to a stale/wrong value after both attempts failed.
    assert CAM_ID not in c._frigate_sticky_port  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fixed_port_uses_base_for_first_cam() -> None:
    """frigate_bind_port > 0: first (only) camera gets the base port exactly."""
    c = _make_coord(frigate_endpoints_enabled=True, frigate_bind_port=8556)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    runner = MagicMock()
    runner.start_server = AsyncMock(return_value=8556)
    c._frigate_runner = runner  # type: ignore[attr-defined]
    await c.async_sync_frigate_endpoint(CAM_ID)
    _, kwargs = runner.start_server.call_args
    assert kwargs["preferred_port"] == 8556
    assert c._frigate_sticky_port[CAM_ID] == 8556  # type: ignore[attr-defined]


CAM_ID_B = "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_fixed_port_second_cam_gets_base_plus_one() -> None:
    """Second camera (sorted cam-ID order) gets base_port + 1."""
    c = _make_coord(frigate_endpoints_enabled=True, frigate_bind_port=8556)
    # data has both cams; CAM_ID < CAM_ID_B lexicographically → CAM_ID is idx 0.
    c.data = {CAM_ID: {}, CAM_ID_B: {}}  # type: ignore[attr-defined]
    c._frigate_high_enabled[CAM_ID_B] = True  # type: ignore[attr-defined]
    runner = MagicMock()
    runner.start_server = AsyncMock(return_value=8557)
    c._frigate_runner = runner  # type: ignore[attr-defined]
    await c.async_sync_frigate_endpoint(CAM_ID_B)
    _, kwargs = runner.start_server.call_args
    assert kwargs["preferred_port"] == 8557


@pytest.mark.asyncio
async def test_fixed_port_bind_error_no_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fixed-port collision logs an error and does NOT fall back to ephemeral."""
    import logging

    c = _make_coord(frigate_endpoints_enabled=True, frigate_bind_port=8556)
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    runner = MagicMock()
    runner.start_server = AsyncMock(side_effect=OSError("port in use"))
    c._frigate_runner = runner  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR, logger="custom_components.bosch_shc_camera"):
        await c.async_sync_frigate_endpoint(CAM_ID)
    assert runner.start_server.call_count == 1  # no retry
    assert CAM_ID not in c._frigate_sticky_port  # type: ignore[attr-defined]
    assert "fixed port" in caplog.text


# ── _frigate_resolve_inner ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_inner_local_ok() -> None:
    c = _make_coord()
    c.try_live_connection = AsyncMock(return_value={})  # type: ignore[attr-defined]
    c._live_connections = {  # type: ignore[attr-defined]
        CAM_ID: {
            "_connection_type": "LOCAL",
            "_local_user": "u",
            "_local_password": "p",
        }
    }
    c._tls_proxy_ports = {CAM_ID: 54321}  # type: ignore[attr-defined]
    target = await c._frigate_resolve_inner(CAM_ID)
    assert target == InnerTarget(54321, "u", "p")


@pytest.mark.asyncio
async def test_resolve_inner_remote_returns_none() -> None:
    c = _make_coord()
    c.try_live_connection = AsyncMock(return_value={})  # type: ignore[attr-defined]
    c._live_connections = {CAM_ID: {"_connection_type": "REMOTE"}}  # type: ignore[attr-defined]
    assert await c._frigate_resolve_inner(CAM_ID) is None


@pytest.mark.asyncio
async def test_resolve_inner_missing_creds_returns_none() -> None:
    c = _make_coord()
    c.try_live_connection = AsyncMock(return_value={})  # type: ignore[attr-defined]
    c._live_connections = {  # type: ignore[attr-defined]
        CAM_ID: {
            "_connection_type": "LOCAL",
            "_local_user": "u",
            "_local_password": "p",
        }
    }
    c._tls_proxy_ports = {}  # type: ignore[attr-defined]  # no inner proxy port
    assert await c._frigate_resolve_inner(CAM_ID) is None


# ── async_stop_frigate_endpoints ─────────────────────────────────────────────


def test_stop_endpoints() -> None:
    c = _make_coord()
    runner = MagicMock()
    c._frigate_runner = runner  # type: ignore[attr-defined]
    c.async_stop_frigate_endpoints()
    runner.stop_all.assert_called_once()
    assert c._frigate_runner is None  # type: ignore[attr-defined]
    # idempotent
    c.async_stop_frigate_endpoints()


# ── _has_active_consumer honours a connected recorder ────────────────────────


@pytest.mark.asyncio
async def test_has_active_consumer_true_when_recorder_connected() -> None:
    """A recorder on the front-door must keep the session alive (no reaping)."""
    c = SimpleNamespace(
        _nvr_processes={},
        _camera_entities={CAM_ID: SimpleNamespace(stream=None)},
        _frigate_runner=SimpleNamespace(active_count=lambda _c: 1),
        _go2rtc_consumer_count=AsyncMock(return_value=0),
    )
    assert await Coordinator._has_active_consumer(c, CAM_ID) is True
    c._go2rtc_consumer_count.assert_not_awaited()  # frigate short-circuits


# ─────────────────────────────────────────────────────────────────────────────
# Switches
# ─────────────────────────────────────────────────────────────────────────────


def _switch_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"}
            }
        },
        _frigate_high_enabled={},
        _frigate_low_enabled={},
        last_update_success=True,
        async_sync_frigate_endpoint=AsyncMock(),
        async_update_listeners=MagicMock(),
    )


@pytest.mark.asyncio
async def test_frigate_switches_default_off(stub_entry) -> None:
    from custom_components.bosch_shc_camera.switch import (
        BoschFrigateHighSwitch,
        BoschFrigateLowSwitch,
    )

    coord = _switch_coord()
    high = BoschFrigateHighSwitch(coord, CAM_ID, stub_entry)
    low = BoschFrigateLowSwitch(coord, CAM_ID, stub_entry)
    assert high._attr_entity_registry_enabled_default is False
    assert high.is_on is False and low.is_on is False
    assert high._quality == "high" and low._quality == "low"
    assert high.available is True


@pytest.mark.asyncio
async def test_frigate_high_switch_toggles_only_high(stub_entry) -> None:
    from custom_components.bosch_shc_camera.switch import BoschFrigateHighSwitch

    coord = _switch_coord()
    entity = BoschFrigateHighSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()
    assert coord._frigate_high_enabled[CAM_ID] is True
    assert coord._frigate_low_enabled == {}  # untouched
    coord.async_sync_frigate_endpoint.assert_called_with(CAM_ID)

    await entity.async_turn_off()
    assert coord._frigate_high_enabled[CAM_ID] is False


@pytest.mark.asyncio
async def test_frigate_low_switch_uses_low_store(stub_entry) -> None:
    from custom_components.bosch_shc_camera.switch import BoschFrigateLowSwitch

    coord = _switch_coord()
    entity = BoschFrigateLowSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()
    await entity.async_turn_on()
    assert coord._frigate_low_enabled[CAM_ID] is True
    assert coord._frigate_high_enabled == {}


@pytest.mark.asyncio
async def test_frigate_switch_restores_on(stub_entry) -> None:
    from custom_components.bosch_shc_camera.switch import BoschFrigateHighSwitch

    coord = _switch_coord()
    entity = BoschFrigateHighSwitch(coord, CAM_ID, stub_entry)
    entity.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="on"))

    async def _noop(self: object) -> None:
        return None

    # Skip RestoreEntity parent wiring (mixin chain) — focus on restore logic.
    entity.__class__.__mro__[2].async_added_to_hass = _noop  # type: ignore[method-assign]
    await entity.async_added_to_hass()
    assert coord._frigate_high_enabled[CAM_ID] is True
    coord.async_sync_frigate_endpoint.assert_called_with(CAM_ID)


# ─────────────────────────────────────────────────────────────────────────────
# Sensors
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_frigate_url_sensors(stub_entry) -> None:
    from custom_components.bosch_shc_camera.sensor import (
        BoschFrigateUrlHighSensor,
        BoschFrigateUrlLowSensor,
    )

    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"}
            }
        },
        frigate_endpoint_url=MagicMock(
            side_effect=lambda _c, q: (
                f"rtsp://h:8600/rtsp_tunnel?inst={'1' if q == 'high' else '2'}"
            )
        ),
        last_update_success=True,
    )
    high = BoschFrigateUrlHighSensor(coord, CAM_ID, stub_entry)
    low = BoschFrigateUrlLowSensor(coord, CAM_ID, stub_entry)
    assert high._attr_entity_registry_enabled_default is False
    assert high.native_value.endswith("inst=1")
    assert low.native_value.endswith("inst=2")


@pytest.mark.asyncio
async def test_frigate_url_sensor_none(stub_entry) -> None:
    from custom_components.bosch_shc_camera.sensor import BoschFrigateUrlHighSensor

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "T", "hardwareVersion": "HOME_Eyes_Outdoor"}}},
        frigate_endpoint_url=MagicMock(return_value=None),
        last_update_success=True,
    )
    assert BoschFrigateUrlHighSensor(coord, CAM_ID, stub_entry).native_value is None


# ── HA#37 regression: _frigate_resolve_inner must not call try_live_connection
#    when a LOCAL session is already active (Gen2 FW rotates creds on every PUT)


@pytest.mark.asyncio
async def test_resolve_inner_skips_try_live_when_local_active() -> None:
    """Bug: unconditional try_live_connection → cred rotation → stream drop loop.

    When a LOCAL session is already active, _frigate_resolve_inner must NOT call
    try_live_connection (which issues PUT /connection, rotating Digest creds on
    Gen2 FW 9.40.25+). Confirms HA#37 fix.
    """
    c = _make_coord()
    c.try_live_connection = AsyncMock(return_value={})  # type: ignore[attr-defined]
    c._live_connections = {  # type: ignore[attr-defined]
        CAM_ID: {
            "_connection_type": "LOCAL",
            "_local_user": "u",
            "_local_password": "p",
        }
    }
    c._tls_proxy_ports = {CAM_ID: 12345}  # type: ignore[attr-defined]
    target = await c._frigate_resolve_inner(CAM_ID)
    assert target == InnerTarget(12345, "u", "p")
    # Must NOT have called try_live_connection — doing so rotates Gen2 creds.
    c.try_live_connection.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_inner_opens_session_when_no_local() -> None:
    """When no LOCAL session exists, _frigate_resolve_inner must call try_live_connection."""
    c = _make_coord()

    async def _set_local(_cam_id: str, **_kw: object) -> dict:
        c._live_connections[_cam_id] = {  # type: ignore[attr-defined]
            "_connection_type": "LOCAL",
            "_local_user": "u",
            "_local_password": "p",
        }
        return {}

    c.try_live_connection = AsyncMock(side_effect=_set_local)  # type: ignore[attr-defined]
    c._live_connections = {}  # type: ignore[attr-defined]  # no session yet
    c._tls_proxy_ports = {CAM_ID: 54321}  # type: ignore[attr-defined]
    target = await c._frigate_resolve_inner(CAM_ID)
    assert target == InnerTarget(54321, "u", "p")
    c.try_live_connection.assert_called_once_with(CAM_ID)


# ── frigate_max_connections option ──────────────────────────────────────────


def test_frigate_config_max_connections_default() -> None:
    """frigate_max_connections defaults to 8 when the option is absent."""
    c = _make_coord()
    cfg = c._frigate_config()
    assert cfg.max_connections == 8


def test_frigate_config_max_connections_custom() -> None:
    """frigate_max_connections is read from options and forwarded to FrontDoorConfig."""
    c = _make_coord(frigate_max_connections=4)
    cfg = c._frigate_config()
    assert cfg.max_connections == 4


def test_front_door_config_max_connections_field() -> None:
    """FrontDoorConfig.max_connections is honoured by _CameraServer semaphore size."""
    from custom_components.bosch_shc_camera.frigate_endpoint import _CameraServer

    cfg = FrontDoorConfig(max_connections=3)
    # Instantiate with dummy callbacks — the server socket is not bound here.
    server = _CameraServer(CAM_ID, cfg, AsyncMock(), None, None)
    # Semaphore value equals max_connections.
    assert server._sem._value == 3  # type: ignore[attr-defined]


# ── HIGH/LOW quality isolation: each switch controls only its own URL ─────────


def _make_running_coord(**extra_opts: object) -> object:
    """Coordinator with feature on + fake runner bound on port 8600."""
    c = _make_coord(frigate_endpoints_enabled=True, **extra_opts)
    c._frigate_runner = SimpleNamespace(  # type: ignore[attr-defined]
        has_server=lambda _c: True,
        port=lambda _c: 8600,
    )
    return c


def test_frigate_switch_always_available(stub_entry) -> None:
    """Frigate switch must be always available (not tied to coordinator success).

    Root cause of HA#37 'Unknown' URL: when the camera went offline, the switch
    became 'unavailable'; RestoreEntity saved that state; after restart
    async_get_last_state() returned 'unavailable' (not 'on'), so the front-door
    was never restarted and the URL sensor stayed 'Unknown' permanently.

    Fix: switch.available always returns True — it is a CONFIG entity, not a
    status entity.
    """
    from custom_components.bosch_shc_camera.switch import BoschFrigateHighSwitch

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "T", "hardwareVersion": "HOME_Eyes_Outdoor"}}},
        last_update_success=False,  # coordinator failed — camera offline
    )
    sw = BoschFrigateHighSwitch(coord, CAM_ID, stub_entry)
    assert sw.available is True  # Must be True even when coordinator is failing


def test_high_switch_on_only_exposes_high_url() -> None:
    """High switch ON → high URL returned; low URL is None (inst=2 not enabled)."""
    c = _make_running_coord()
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    # low not set → falsy
    high_url = c.frigate_endpoint_url(CAM_ID, "high")
    low_url = c.frigate_endpoint_url(CAM_ID, "low")
    assert high_url is not None and "inst=1" in high_url
    assert low_url is None


def test_low_switch_on_only_exposes_low_url() -> None:
    """Low switch ON → low URL returned; high URL is None (inst=1 not enabled)."""
    c = _make_running_coord()
    c._frigate_low_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    # high not set → falsy
    high_url = c.frigate_endpoint_url(CAM_ID, "high")
    low_url = c.frigate_endpoint_url(CAM_ID, "low")
    assert high_url is None
    assert low_url is not None and "inst=2" in low_url


def test_both_switches_on_exposes_both_urls() -> None:
    """Both HIGH and LOW switches ON → both quality URLs are returned."""
    c = _make_running_coord()
    c._frigate_high_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    c._frigate_low_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    high_url = c.frigate_endpoint_url(CAM_ID, "high")
    low_url = c.frigate_endpoint_url(CAM_ID, "low")
    assert high_url is not None and "inst=1" in high_url
    assert low_url is not None and "inst=2" in low_url


def test_high_switch_off_hides_high_url_even_when_low_on() -> None:
    """Turning HIGH switch OFF hides its URL; LOW switch state is unaffected."""
    c = _make_running_coord()
    c._frigate_high_enabled[CAM_ID] = False  # type: ignore[attr-defined]
    c._frigate_low_enabled[CAM_ID] = True  # type: ignore[attr-defined]
    assert c.frigate_endpoint_url(CAM_ID, "high") is None
    assert c.frigate_endpoint_url(CAM_ID, "low") is not None


# ─────────────────────────────────────────────────────────────────────────────
# _frigate_on_idle (bug-hunt 2026-07-01, C5): the on_idle callback the front-door
# fires after `frigate_idle_timeout`s of zero recorder clients. Must tear down
# the on-demand LOCAL session ONLY if nothing else still needs it.
# ─────────────────────────────────────────────────────────────────────────────


def _make_idle_coord(*, active_consumer: bool, live: dict | None) -> SimpleNamespace:
    hass = SimpleNamespace(
        async_create_task=lambda coro, name=None: asyncio.ensure_future(coro)
    )
    return SimpleNamespace(
        hass=hass,
        _bg_tasks=set(),
        _has_active_consumer=AsyncMock(return_value=active_consumer),
        _live_connections=({CAM_ID: live} if live is not None else {}),
        _tear_down_live_stream=AsyncMock(),
    )


async def _run_on_idle(coord: SimpleNamespace) -> None:
    Coordinator._frigate_on_idle(coord, CAM_ID)
    task = next(iter(coord._bg_tasks))
    await task


async def test_frigate_on_idle_skips_teardown_when_another_consumer_is_active() -> None:
    """An active card view / Cast / Mini-NVR keeps the session — no teardown."""
    coord = _make_idle_coord(active_consumer=True, live={"_connection_type": "LOCAL"})
    await _run_on_idle(coord)
    coord._tear_down_live_stream.assert_not_called()


async def test_frigate_on_idle_skips_teardown_when_no_local_session() -> None:
    """No consumer, but nothing LOCAL to tear down either (REMOTE or absent)."""
    coord = _make_idle_coord(active_consumer=False, live={"_connection_type": "REMOTE"})
    await _run_on_idle(coord)
    coord._tear_down_live_stream.assert_not_called()

    coord2 = _make_idle_coord(active_consumer=False, live=None)
    await _run_on_idle(coord2)
    coord2._tear_down_live_stream.assert_not_called()


async def test_frigate_on_idle_tears_down_local_session_when_truly_idle() -> None:
    """No other consumer + an on-demand LOCAL session → tear it down."""
    coord = _make_idle_coord(active_consumer=False, live={"_connection_type": "LOCAL"})
    await _run_on_idle(coord)
    coord._tear_down_live_stream.assert_called_once_with(CAM_ID)
