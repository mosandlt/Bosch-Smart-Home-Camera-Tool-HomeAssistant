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
        "frigate_ip_allowlist": "",
        "frigate_auth_mode": "none",
        "frigate_token": "",
        "frigate_basic_user": "frigate",
        "frigate_idle_timeout": 60,
    }
    opts.update(options)
    c = _CoordDouble()
    c.options = opts  # type: ignore[attr-defined]
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
    )
    cfg = c._frigate_config()
    assert isinstance(cfg, FrontDoorConfig)
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.ip_allowlist == frozenset({"192.168.1.5", "10.0.0.0/8"})
    assert cfg.auth_mode == "basic"
    assert cfg.token == "secret"
    assert cfg.basic_user == "rec"
    assert cfg.idle_timeout == 120.0


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
