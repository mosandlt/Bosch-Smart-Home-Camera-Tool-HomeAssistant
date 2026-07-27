"""Chaos-engineering / fault-injection test suite.

Systematic error injection at unit/integration level (NO live infrastructure,
NO real network I/O) proving the integration NEVER crashes with an
unhandled exception on a random/systematic fault at an await point, and
ALWAYS self-heals on the next coordinator tick / next call — no
permanently-broken state, no leaked locks, no hung event-loop tasks.

Seven focus areas, one class (or a small group of classes) each:
  1. Cloud-API faults (timeout/ClientError/5xx/429) at the camera-list
     fetch, per-camera status/events, RCP slow-tier.
  2. TLS-proxy / RTSP hard connection resets (tls_proxy.py).
  3. go2rtc unreachable while a camera is actively streaming
     (go2rtc_client.py `unregister_go2rtc_stream`, WP1 teardown-race
     RuntimeError path — the manual `register_go2rtc_stream` PUT this area
     used to also cover was removed 2026-07-14, HA-Core-submission-prep,
     superseded by HA-core's own native go2rtc auto-registration).
  4. Token-refresh cascade failures (token_auth.py) — escalation to
     ConfigEntryAuthFailed, never hangs, self-heals.
  5. Camera-removal race (`_purge_cam_id` vs. an in-flight fetch for the
     same cam_id).
  6. SMB/FTP NVR-upload unreachable (smb.py `socket.setdefaulttimeout`
     hardening, no executor-thread hang).

Test-double conventions (deliberately copied, not reinvented, from
tests/test_init.py's SimpleNamespace-based coordinator stubs —
`_make_coord_sprint_kb`/`_url_session`/`_make_resp_sprint_kb`/
`_make_coord_token_refresh`): a
coordinator is a `SimpleNamespace` (or, where the real class body is
needed — `_purge_cam_id`'s audited attribute tuples — a
`BoschCameraCoordinator.__new__(BoschCameraCoordinator)` bare instance)
carrying only the attributes the exercised method actually reads, and
`BoschCameraCoordinator._method(stub, ...)` unbound-style calls exercise
the REAL production method against that stub.
"""

from __future__ import annotations

import asyncio
import random
import socket
import ssl
import struct
import sys
import threading
import time
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera import tls_proxy as _tls_proxy_mod
from custom_components.bosch_shc_camera.go2rtc_client import (
    unregister_go2rtc_stream,
)
from custom_components.bosch_shc_camera.session_state import (
    BoolFieldView,
    CacheFieldView,
)
from custom_components.bosch_shc_camera.tls_proxy import start_tls_proxy, stop_tls_proxy

# Captured BEFORE any patch.object() call below replaces
# `asyncio.open_connection` — `_tls_proxy_mod.asyncio` is the same module
# object as the global `asyncio`, so patching it patches this name too.
# Calling the (unpatched) real function through this reference avoids
# infinite recursion. (Same pattern as tests/test_tls_proxy.py.)
_real_open_connection = asyncio.open_connection


async def _no_tls_open_connection(
    host: str, port: int, *, ssl: ssl.SSLContext | None = None, **kwargs: object
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Stand-in for `asyncio.open_connection` that skips the TLS handshake —
    these chaos tests have no real cert for the fake-camera loopback
    server, and the proxy's own TLS-connect logic isn't what's under test
    here (that's `ssl`'s job); only the relay/circuit-breaker behavior is."""
    return await _real_open_connection(host, port)


CAM_A = "11111111-1111-1111-1111-111111111111"
CAM_B = "22222222-2222-2222-2222-222222222222"

_PATCH_SESSION = "custom_components.bosch_shc_camera.async_get_bosch_cloud_session"


# Shared coordinator-stub / fake-response helpers.
# Mirrors tests/test_init.py's `_make_resp_sprint_kb` / `_url_session` /
# `_make_coord_sprint_kb` verbatim in shape (kept self-contained in this
# file rather than cross-imported, so this chaos suite has no dependency on
# test_init.py's 37k-line internal layout).


def _make_resp(status: int, json_val=None, text_val: str = ""):
    """Context-manager-compatible fake aiohttp response."""
    r = MagicMock()
    r.status = status
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=None)
    r.json = AsyncMock(return_value=json_val if json_val is not None else {})
    r.text = AsyncMock(return_value=text_val)
    return r


CAM_LIST = [{"id": CAM_A, "hardwareVersion": "HOME_Eyes_Outdoor"}]


def _base_url_map(**extras):
    m = {"__cam_list__": _make_resp(200, json_val=CAM_LIST)}
    m.update(extras)
    return m


def _url_session(url_map: dict):
    """Healthy/fixed-response session — routes GET by URL substring."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    cam_list_resp = url_map.get("__cam_list__")
    pattern_items = [(k, v) for k, v in url_map.items() if k != "__cam_list__"]
    sorted_patterns = sorted(pattern_items, key=lambda kv: len(kv[0]), reverse=True)

    def _get(url, **kwargs):
        path = url.split("?")[0]
        if cam_list_resp is not None and path.endswith("/video_inputs"):
            return cam_list_resp
        for pattern, resp in sorted_patterns:
            if pattern in url:
                return resp
        return _make_resp(200, json_val=[], text_val="")

    session.get = _get
    session.put = MagicMock(return_value=_make_resp(404, json_val={}))
    return session


def _random_fault_or_ok(rng: random.Random):
    """Chaos primitive: either a healthy 200 or one of several realistic
    Bosch-cloud fault modes, chosen pseudo-randomly from a seeded RNG so
    every test run is reproducible."""
    choice = rng.choice(["ok", "timeout", "client_error", "500", "503", "429"])
    if choice == "ok":
        return _make_resp(200, json_val={}, text_val='"ONLINE"')
    if choice == "timeout":
        raise TimeoutError("chaos: cloud endpoint timed out")
    if choice == "client_error":
        raise aiohttp.ClientError("chaos: connection reset by peer")
    if choice == "500":
        return _make_resp(500, json_val={}, text_val="Internal Server Error")
    if choice == "503":
        return _make_resp(503, json_val={}, text_val="Service Unavailable")
    return _make_resp(429, json_val={}, text_val="Too Many Requests")


def _chaos_url_session(url_map: dict, rng: random.Random):
    """Like `_url_session`, but every per-camera/slow-tier/RCP GET (and
    every PUT — the RCP-connection call reuses this same session) resolves
    to a randomized fault or a healthy response. The top-level camera-list
    endpoint stays healthy — top-level chaos is covered separately by
    `TestCloudApiFaultInjectionCameraList` since it has different
    (propagate-as-UpdateFailed) semantics than the per-camera layer
    (swallow-and-continue)."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    cam_list_resp = url_map.get("__cam_list__")
    patterns = [k for k in url_map if k != "__cam_list__"]

    def _get(url, **kwargs):
        path = url.split("?")[0]
        if cam_list_resp is not None and path.endswith("/video_inputs"):
            return cam_list_resp
        for pattern in patterns:
            if pattern in url:
                return _random_fault_or_ok(rng)
        return _make_resp(200, json_val=[], text_val="")

    session.get = _get

    def _put(url, **kwargs):
        return _random_fault_or_ok(rng)

    session.put = MagicMock(side_effect=_put)
    return session


def _make_coord(**overrides):
    """Full coordinator stub for `_async_update_data` — sets every
    attribute the method may touch on a healthy run. Trimmed copy of
    tests/test_init.py's `_make_coord_sprint_kb` (same shape, kept
    self-contained here)."""

    def _create_task(coro, **kwargs):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock(spec=asyncio.Task)

    base = dict(
        entry=SimpleNamespace(
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={},
            entry_id="01CHAOSENTRY000000000000",
        ),
        _refreshed_token=None,
        _refreshed_refresh=None,
        token="tok-A",
        refresh_token="rfr-B",
        options={},
        feature_flags={"dummy": True},
        protocol_checked=True,
        fcm_lock=threading.Lock(),
        fcm_running=False,
        fcm_healthy=True,
        fcm_client=None,
        _last_status=float("-inf"),
        _last_events=float("-inf"),
        _last_slow=time.monotonic(),
        last_smb_cleanup=time.monotonic(),
        last_nvr_cleanup=time.monotonic(),
        hw_version={},
        cached_status={},
        cached_events={},
        commissioned_cache={},
        offline_since={},
        per_cam_status_at={},
        stream_fell_back={},
        stream_error_count={},
        stream_error_at={},
        live_connections={},
        local_promote_at={},
        lan_tcp_reachable={},
        rcp_lan_ip_cache={},
        local_creds_cache={},
        shc_state_cache={},
        wifiinfo_cache={},
        last_event_ids={},
        _event_dedup_cache={},
        alert_sent_ids={},
        pan_cache={},
        lighting_switch_cache={},
        privacy_set_at={},
        light_set_at={},
        notif_set_at={},
        privacy_sound_set_at={},
        timestamp_set_at={},
        ledlights_set_at={},
        integration_version="chaos-test",
        _OFFLINE_EXTENDED_INTERVAL=900,
        WRITE_LOCK_SECS=30.0,
        _session_quota_hits={},
        _SESSION_QUOTA_WINDOW_S=300.0,
        _SESSION_QUOTA_NOTIFY_THRESHOLD=3,
        shc_ready=False,
        camera_entities={},
        _stream_locks={},
        tls_proxy_ports={},
        audio_enabled={},
        session_stale={},
        renewal_tasks={},
        bg_tasks=set(),
        nvr_processes={},
        nvr_user_intent={},
        rcp_session_cache={},
        # ── slow-tier per-endpoint caches (slow_tier.py) ─────────────────────
        ambient_light_cache={},
        ambient_lighting_cache={},
        alarm_settings_cache={},
        alarm_settings_set_at={},
        alarm_status_cache={},
        arming_cache={},
        arming_set_at={},
        audio_cache={},
        audio_detection_cache={},
        audio_detection_set_at={},
        cloud_privacy_masks_cache={},
        cloud_zones_cache={},
        firmware_cache={},
        firmware_set_at={},
        gen2_private_areas_cache={},
        gen2_zones_cache={},
        global_lighting_cache={},
        icon_led_brightness_cache={},
        intrusion_config_cache={},
        intrusion_config_set_at={},
        ledlights_cache={},
        lens_elevation_cache={},
        lighting_options_cache={},
        lighting_options_set_at={},
        motion_light_cache={},
        motion_set_at={},
        notifications_cache={},
        privacy_sound_cache={},
        rules_cache={},
        timestamp_cache={},
        unread_events_cache={},
        ensure_valid_token=AsyncMock(return_value="fresh-tok"),
        _async_update_shc_states=AsyncMock(),
        _async_update_rcp_data=AsyncMock(),
        _async_update_rcp_data_for_cam=AsyncMock(),
        async_mark_events_read=AsyncMock(),
        is_write_locked=MagicMock(return_value=False),
        cleanup_stale_devices=MagicMock(),
        tear_down_live_stream=AsyncMock(),
        promote_to_local=AsyncMock(),
        async_send_alert=AsyncMock(),
        async_local_tcp_ping=AsyncMock(return_value=False),
        should_check_status=MagicMock(return_value=True),
        get_cam_lan_ip=MagicMock(return_value=None),
        get_model_config=lambda cid: SimpleNamespace(generation=2),
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            async_create_background_task=MagicMock(side_effect=_create_task),
            async_add_executor_job=AsyncMock(),
            bus=SimpleNamespace(async_fire=MagicMock()),
            data={},
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(path=lambda *a: "/tmp"),
            config_entries=SimpleNamespace(async_reload=AsyncMock()),
        ),
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    ns._first_tick_done = (
        True  # simulate second+ tick (do_events/do_slow not force-disabled)
    )
    if not hasattr(ns, "bg_tasks"):
        ns.bg_tasks = set()
    ns.spawn_tracked = __import__("types").MethodType(
        BoschCameraCoordinator.spawn_tracked, ns
    )
    return ns


class TestCloudApiFaultInjectionCameraList:
    """Top-level `GET /v11/video_inputs` (fetch_camera_list, camera_list.py)
    is NOT swallowed by design — it propagates as `UpdateFailed` so HA's
    DataUpdateCoordinator can mark the entry unavailable and retry on its
    own schedule (a `ConfigEntryAuthFailed` on the 401-then-401-again path
    is functionally the same "clean recoverable failure" contract). The
    chaos assertion is narrower than the per-camera layer: never let a
    fault escape as a RAW/unclassified exception, and always recover
    cleanly on the very next tick.
    """

    def _broken_camera_list_session(self, exc: Exception) -> MagicMock:
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=exc)
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(return_value=cm)
        return session

    async def test_timeout_raises_update_failed_not_raw_exception(self):
        """`fetch_camera_list` retries a bare timeout once (real delay —
        `VIDEO_INPUTS_RETRY_DELAY_SEC` — patched out here so this stays a
        fast unit test) before letting it propagate as `UpdateFailed`."""
        coord = _make_coord()
        session = self._broken_camera_list_session(
            TimeoutError("chaos: cam-list timeout")
        )
        with (
            patch(_PATCH_SESSION, new=AsyncMock(return_value=session)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(UpdateFailed):
                await asyncio.wait_for(
                    BoschCameraCoordinator._async_update_data(coord), timeout=5.0
                )

    async def test_client_connection_error_raises_update_failed_not_raw_exception(self):
        coord = _make_coord()
        session = self._broken_camera_list_session(
            aiohttp.ClientConnectionError("chaos: connection refused")
        )
        with patch(_PATCH_SESSION, new=AsyncMock(return_value=session)):
            with pytest.raises(UpdateFailed):
                await asyncio.wait_for(
                    BoschCameraCoordinator._async_update_data(coord), timeout=5.0
                )

    async def test_outage_burst_then_recovery_self_heals(self):
        """3 consecutive UpdateFailed ticks (simulating a real Bosch-cloud
        outage burst, alternating fault types), then a healthy tick — the
        coordinator must not accumulate any broken state (stuck timestamps,
        held locks) that blocks the healthy tick from succeeding."""
        coord = _make_coord()
        faults = [
            TimeoutError("chaos: outage tick 1"),
            aiohttp.ClientConnectionError("chaos: outage tick 2"),
            TimeoutError("chaos: outage tick 3"),
        ]
        for exc in faults:
            session = self._broken_camera_list_session(exc)
            with (
                patch(_PATCH_SESSION, new=AsyncMock(return_value=session)),
                patch("asyncio.sleep", new=AsyncMock()),
            ):
                with pytest.raises(UpdateFailed):
                    await asyncio.wait_for(
                        BoschCameraCoordinator._async_update_data(coord), timeout=5.0
                    )

        healthy_session = _url_session(_base_url_map())
        with patch(_PATCH_SESSION, new=AsyncMock(return_value=healthy_session)):
            result = await asyncio.wait_for(
                BoschCameraCoordinator._async_update_data(coord), timeout=5.0
            )
        assert isinstance(result, dict)
        assert CAM_A in result


class TestCloudApiFaultInjectionPerCameraChaos:
    """The per-camera layer — status/events polling, `_poll_slow_tier_endpoints`,
    and the inline RCP-connection PUT (all in `__init__.py` /
    `slow_tier.py`) — swallow per-endpoint failures internally
    (`asyncio.gather(..., return_exceptions=True)` or a local
    try/except). A single camera's cloud blip must never abort the whole
    tick, and the tick itself must always return a clean dict, never raise.
    """

    async def test_random_faults_across_every_per_cam_endpoint_never_crash_tick(self):
        rng = random.Random(
            20260713
        )  # reproducible chaos (SENTINEL-adjacent: fixed seed)
        coord = _make_coord(_last_slow=float("-inf"))  # force slow-tier this tick
        url_map = _base_url_map(
            **{
                f"/{CAM_A}/ping": None,
                f"/{CAM_A}/commissioned": None,
                f"/{CAM_A}/events": None,
                f"/{CAM_A}/wifiinfo": None,
                f"/{CAM_A}/motion": None,
                f"/{CAM_A}/audioAlarm": None,
                f"/{CAM_A}/lighting": None,
                f"/{CAM_A}/connection": None,
            }
        )
        chaos_session = _chaos_url_session(url_map, rng)

        with patch(_PATCH_SESSION, new=AsyncMock(return_value=chaos_session)):
            result = await asyncio.wait_for(
                BoschCameraCoordinator._async_update_data(coord), timeout=10.0
            )
        assert isinstance(result, dict)
        assert CAM_A in result

        # Self-heal: the very next tick, with a fully healthy session, must
        # complete cleanly — no leftover write-locks / stuck timestamps /
        # degraded state carried over from the chaos tick.
        healthy_session = _url_session(
            _base_url_map(
                **{
                    f"/{CAM_A}/ping": _make_resp(200, text_val='"ONLINE"'),
                    f"/{CAM_A}/commissioned": _make_resp(200, {"commissioned": True}),
                }
            )
        )
        with patch(_PATCH_SESSION, new=AsyncMock(return_value=healthy_session)):
            result2 = await asyncio.wait_for(
                BoschCameraCoordinator._async_update_data(coord), timeout=10.0
            )
        assert isinstance(result2, dict)
        assert CAM_A in result2

    async def test_repeated_chaos_ticks_never_raise_and_lock_state_stays_clean(self):
        """10 consecutive chaos ticks in a row (not just one) — the
        systematic version of the above: no accumulation of stuck
        `is_write_locked`/`bg_tasks` state across many bad ticks."""
        rng = random.Random(4242)
        coord = _make_coord()
        url_map = _base_url_map(
            **{
                f"/{CAM_A}/ping": None,
                f"/{CAM_A}/commissioned": None,
                f"/{CAM_A}/events": None,
            }
        )
        for i in range(10):
            coord._last_slow = float("-inf") if i % 3 == 0 else time.monotonic()
            session = _chaos_url_session(url_map, rng)
            with patch(_PATCH_SESSION, new=AsyncMock(return_value=session)):
                result = await asyncio.wait_for(
                    BoschCameraCoordinator._async_update_data(coord), timeout=10.0
                )
            assert isinstance(result, dict)
        # No background tasks were left dangling across the whole burst.
        assert coord.bg_tasks == set() or all(
            getattr(t, "done", lambda: True)() for t in coord.bg_tasks
        )


@pytest.fixture(autouse=True)
def _enable_loopback_sockets_for_chaos(
    socket_enabled: None,
) -> Generator[None, None, None]:
    """pytest-homeassistant-custom-component blocks real `socket.socket()`
    calls by default; the TLS-proxy tests below legitimately need real
    127.0.0.1 loopback sockets (real accept-loop thread + real client
    sockets triggering a real OS-level RST)."""
    yield


async def _streaming_fake_camera(reader, writer):
    """Loopback camera double that, unlike a plain echo server, also
    proactively pushes small periodic chunks even with no client data —
    matching a REAL Bosch RTSP camera's continuous outbound RTP stream.

    This matters specifically for the hard-reset chaos tests below:
    `_pipe`'s CAM→C direction (`tls_proxy.py`) has NO read timeout by
    design (dark/still scenes have sparse RTP, so a timeout there would
    misfire) — it only notices a client that hard-reset once it has real
    camera-side data to try to relay and `dst.sendall()` fails. A
    perfectly silent test double would leave that thread blocked in
    `select()` indefinitely (proven via a stack-trace capture during this
    suite's development — a real characteristic of the no-timeout
    direction, not a test bug), which a real streaming camera never
    triggers in practice. Simulating the camera's real behavior here
    keeps the test honest without touching the production hot path.
    """
    try:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=0.15)
            except TimeoutError:
                writer.write(b"\x24\x00\x00\x04RTP0")  # fake RTP-interleaved chunk
                await writer.drain()
                continue
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


class TestTlsProxyHardResetRecovery:
    """A mid-relay hard socket reset (ECONNRESET via `SO_LINGER(1,0)`, not
    a clean FIN) on one client connection must not kill the proxy's
    asyncio accept loop — the NEXT client connection to the same proxy
    port must still be served normally. This is the TLS-proxy equivalent
    of "self-heals on the next tick".

    tls_proxy.py is asyncio-native (`asyncio.start_server`, no daemon
    threads) as of the 2026-07 HA-Core-submission rewrite — these tests
    drive the real proxy the same way tests/test_tls_proxy.py does:
    `asyncio.open_connection` is patched to skip the TLS handshake (no
    real cert for the fake-camera loopback server), while INBOUND client
    connections use raw, unpatched `socket.create_connection` so the
    hard-reset (SO_LINGER) is a genuine OS-level RST hitting the proxy's
    accepted connection, not anything mocked."""

    async def test_hard_reset_mid_relay_does_not_kill_accept_loop(self):
        cam_id = "CHAOS-TLS-RESET-A"

        srv = await asyncio.start_server(_streaming_fake_camera, "127.0.0.1", 0)
        cam_port = srv.sockets[0].getsockname()[1]
        port_cache: dict[str, int] = {}
        server_cache: dict[str, asyncio.base_events.Server] = {}
        ctx = ssl.create_default_context()
        try:
            with patch.object(
                _tls_proxy_mod.asyncio, "open_connection", _no_tls_open_connection
            ):
                port = await start_tls_proxy(
                    ctx, cam_id, "127.0.0.1", cam_port, port_cache, server_cache
                )
                await asyncio.sleep(0.05)

                # Client 1: connect, send a partial RTSP DESCRIBE, then hard
                # reset instead of a clean close — SO_LINGER(on=1, linger=0)
                # forces the OS to send RST instead of FIN on close().
                c1 = socket.create_connection(("127.0.0.1", port), timeout=2)
                c1.sendall(b"DESCRIBE rtsp://cam/stream1 RTSP/1.0\r\nCSeq: 1\r\n\r\n")
                await asyncio.sleep(0.1)
                c1.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                c1.close()

                # Give the proxy's relay coroutines a moment to observe the
                # reset and run their (already-tested-elsewhere) close-once
                # cleanup.
                await asyncio.sleep(0.3)

                # Client 2: a FRESH connection to the SAME proxy port. The
                # accept loop must still be alive and serve it normally —
                # proof the hard reset didn't crash/wedge it.
                c2 = socket.create_connection(("127.0.0.1", port), timeout=2)
                c2.settimeout(2.0)
                c2.sendall(b"PING_AFTER_RESET\r\n\r\n")
                # `_streaming_fake_camera` and the proxy's own relay
                # coroutines only run when THIS test's own event loop gets
                # to spin. A subsequent blocking `socket.recv()` call would
                # otherwise stall that very loop (single-threaded: recv()
                # blocks the OS thread the loop runs on), so the echo could
                # never be scheduled. Yield first so the camera-side echo
                # (and the proxy's relay of it) has already happened and is
                # sitting in the client's kernel receive buffer before we
                # block on it.
                await asyncio.sleep(0.3)
                reply = b""
                try:
                    reply = c2.recv(65536)
                except TimeoutError:
                    pass
                c2.close()
                assert b"PING_AFTER_RESET" in reply, (
                    "TLS proxy accept loop must still serve a fresh connection "
                    "after a prior client hard-reset mid-relay"
                )
        finally:
            srv.close()
            await srv.wait_closed()
            await stop_tls_proxy(cam_id, port_cache, server_cache)

    async def test_burst_of_hard_resets_does_not_trip_connect_circuit_breaker(self):
        """The circuit breaker (`_MAX_BURST`=5 in `_BURST_WINDOW`=30s) only
        counts failed CONNECTS to the upstream camera — a burst of
        client-side relay resets (the connect to the camera already
        succeeded) must never trip it. Proxy must remain usable after 5+
        consecutive client resets."""
        cam_id = "CHAOS-TLS-RESET-BURST"

        srv = await asyncio.start_server(_streaming_fake_camera, "127.0.0.1", 0)
        cam_port = srv.sockets[0].getsockname()[1]
        port_cache: dict[str, int] = {}
        server_cache: dict[str, asyncio.base_events.Server] = {}
        ctx = ssl.create_default_context()
        try:
            with patch.object(
                _tls_proxy_mod.asyncio, "open_connection", _no_tls_open_connection
            ):
                port = await start_tls_proxy(
                    ctx, cam_id, "127.0.0.1", cam_port, port_cache, server_cache
                )
                await asyncio.sleep(0.05)

                for _ in range(6):  # > _MAX_BURST=5
                    c = socket.create_connection(("127.0.0.1", port), timeout=2)
                    c.sendall(b"SETUP rtsp://cam/stream1 RTSP/1.0\r\nCSeq: 1\r\n\r\n")
                    await asyncio.sleep(0.05)
                    c.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                    c.close()
                    await asyncio.sleep(0.05)

                # Proxy must still be alive — a real connect-failure burst
                # would have closed `srv`; these were all successful connects
                # that then reset mid-relay, which the breaker never counts.
                c_final = socket.create_connection(("127.0.0.1", port), timeout=2)
                c_final.settimeout(2.0)
                c_final.sendall(b"STILL_ALIVE\r\n\r\n")
                # See the sibling test above for why this yield is required
                # before a blocking recv(): the fake camera and the proxy's
                # relay are asyncio-based and need this test's own event
                # loop to actually spin.
                await asyncio.sleep(0.3)
                reply = b""
                try:
                    reply = c_final.recv(65536)
                except TimeoutError:
                    pass
                c_final.close()
                assert b"STILL_ALIVE" in reply, (
                    "a burst of client-side relay resets must not trip the "
                    "connect-failure circuit breaker"
                )
        finally:
            srv.close()
            await srv.wait_closed()
            await stop_tls_proxy(cam_id, port_cache, server_cache)


class TestGo2rtcUnreachableWhileStreaming:
    """`unregister_go2rtc_stream` (go2rtc_client.py) must swallow every
    realistic fault (timeout, connection error, generic OSError, and the
    WP1 teardown-race RuntimeError from `_get_go2rtc_session` — commit
    4303e78) and fall back to a clean None-return instead of raising,
    exactly as the docstring promises ("treat like an unreachable
    endpoint, try the next one"). The manual `register_go2rtc_stream` PUT
    this class used to also cover was removed 2026-07-14
    (HA-Core-submission-prep) — superseded by HA-core's own bundled
    go2rtc provider, which auto-registers whatever `stream_source()`
    returns on every WebRTC offer."""

    def _coord(self, *, session=None, teardown_done: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            camera_entities={},
            hass=SimpleNamespace(config=SimpleNamespace(config_dir=None)),
            go2rtc_session=session,
            go2rtc_teardown_done=teardown_done,
        )

    async def test_unregister_survives_mixed_faults(self):
        session = MagicMock()
        session.closed = False
        faults = iter([TimeoutError("chaos"), aiohttp.ClientError("chaos: reset")])

        def _raise_next(*_a, **_kw):
            raise next(faults)

        session.delete = AsyncMock(side_effect=_raise_next)
        coord = self._coord(session=session)

        # Must not raise — unregister_go2rtc_stream returns None either way.
        await asyncio.wait_for(unregister_go2rtc_stream(coord, CAM_A), timeout=5.0)

    async def test_unregister_races_teardown_does_not_raise(self):
        coord = self._coord(session=None, teardown_done=True)
        # A stream teardown racing the final coordinator shutdown must not
        # raise — the go2rtc entry is either already gone or about to be,
        # either way this is a no-op, not a crash.
        await asyncio.wait_for(unregister_go2rtc_stream(coord, CAM_A), timeout=5.0)


def _make_coord_token_chaos(**overrides):
    def _create_task(coro, **kwargs):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock(spec=asyncio.Task)

    base = dict(
        entry=SimpleNamespace(
            data={"bearer_token": "tok-OLD", "refresh_token": "rfr-OLD"}, options={}
        ),
        _refreshed_token=None,
        _refreshed_refresh=None,
        auth_outage_count=0,
        _auth_outage_next_retry_ts=float("-inf"),  # SENTINEL_RULE: never 0.0
        _token_fail_count=0,
        _token_timeout_fail_count=0,
        _token_alert_sent=False,
        _token_still_valid=lambda min_remaining=60: False,
        schedule_token_refresh=MagicMock(),
        _token_refresh_lock=asyncio.Lock(),
        token="tok-OLD",
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            config_entries=SimpleNamespace(async_update_entry=MagicMock()),
        ),
        debug=False,
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    # `ensure_valid_token` (the method under test) internally calls
    # `self._refresh_token_locked(...)` — bind the REAL production method
    # onto this stub so the full lock-then-refresh path actually runs
    # (mirrors tests/test_init.py's `_wire_spawn_tracked` pattern).
    ns._refresh_token_locked = __import__("types").MethodType(
        BoschCameraCoordinator._refresh_token_locked, ns
    )
    # `_refresh_token_locked` internally calls `self._handle_successful_refresh`
    # on a real success — bind that too (backported from the Core PR's
    # Copilot review round 5, 2026-07-27).
    ns._handle_successful_refresh = __import__("types").MethodType(
        BoschCameraCoordinator._handle_successful_refresh, ns
    )
    return ns


class TestTokenRefreshCascadeChaos:
    """Keycloak answers with a mix of transient/hard/outage faults across
    several consecutive refresh attempts. `_token_fail_count` must escalate
    correctly to `ConfigEntryAuthFailed` (never hang, never raise a
    raw/unclassified exception type), the lock must always be released —
    and a subsequent healthy refresh after the escalation must fully
    self-heal (fail counter resets, a fresh token is returned)."""

    async def test_repeated_transient_failures_escalate_then_self_heal(self):
        coord = _make_coord_token_chaos()

        with (
            patch(
                "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.config_flow._do_refresh",
                new=AsyncMock(return_value=None),  # every attempt "completes" but fails
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            for expected_fail_count in (1, 2):
                with pytest.raises(UpdateFailed):
                    await asyncio.wait_for(
                        BoschCameraCoordinator.ensure_valid_token(coord, "tok-OLD"),
                        timeout=3.0,
                    )
                assert coord._token_fail_count == expected_fail_count
                assert not coord._token_refresh_lock.locked()

            with pytest.raises(ConfigEntryAuthFailed):
                await asyncio.wait_for(
                    BoschCameraCoordinator.ensure_valid_token(coord, "tok-OLD"),
                    timeout=3.0,
                )
        assert coord._token_fail_count == 3
        assert not coord._token_refresh_lock.locked(), (
            "lock must never stay held after the reauth-triggering failure"
        )

        # Self-heal: Bosch/Keycloak recovers (or the user re-authenticated)
        # — the very next call must succeed cleanly and reset all failure
        # bookkeeping. Proof the coordinator never gets permanently wedged
        # by a token-refresh outage.
        new_tokens = {"access_token": "tok-RECOVERED", "refresh_token": "rfr-RECOVERED"}
        with (
            patch(
                "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.config_flow._do_refresh",
                new=AsyncMock(return_value=new_tokens),
            ),
        ):
            out = await asyncio.wait_for(
                BoschCameraCoordinator.ensure_valid_token(coord, "tok-OLD"),
                timeout=3.0,
            )
        assert out == "tok-RECOVERED"
        assert coord._token_fail_count == 0
        assert not coord._token_refresh_lock.locked()

    async def test_concurrent_callers_under_mixed_faults_never_deadlock(self):
        """Several coordinator call sites (401-recovery in
        live_connection.py, camera.py, __init__.py's RCP tier, etc.) can
        all hit `ensure_valid_token` around the same moment. Under a mix
        of outage/transient/success faults, none may hang past a bounded
        timeout, none may raise an unclassified exception, and the lock
        must end up free."""
        from custom_components.bosch_shc_camera.config_flow import AuthServerOutageError

        rng = random.Random(20260713)
        coord = _make_coord_token_chaos()
        attempt_n = 0

        async def _mixed_refresh(session, refresh):
            nonlocal attempt_n
            attempt_n += 1
            choice = rng.choice(["outage", "none", "ok"])
            if choice == "outage":
                raise AuthServerOutageError("502 Bad Gateway (chaos)")
            if choice == "none":
                return None
            return {
                "access_token": f"tok-{attempt_n}",
                "refresh_token": f"rfr-{attempt_n}",
            }

        with (
            patch(
                "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.config_flow._do_refresh",
                new=_mixed_refresh,
            ),
            patch("asyncio.sleep", new=AsyncMock()),
            patch("custom_components.bosch_shc_camera.ir.async_create_issue"),
            patch("custom_components.bosch_shc_camera.ir.async_delete_issue"),
        ):
            results = await asyncio.wait_for(
                asyncio.gather(
                    *[
                        BoschCameraCoordinator.ensure_valid_token(coord, "tok-OLD")
                        for _ in range(6)
                    ],
                    return_exceptions=True,
                ),
                timeout=8.0,
            )

        # Nothing hung (the outer wait_for would have raised TimeoutError
        # otherwise) and the lock must be free for the next real caller.
        assert not coord._token_refresh_lock.locked()
        for res in results:
            if isinstance(res, BaseException):
                assert isinstance(res, (UpdateFailed, ConfigEntryAuthFailed)), (
                    "unexpected unhandled exception type escaped the "
                    f"token-refresh cascade: {res!r}"
                )
            else:
                assert isinstance(res, str) and res


def _make_purge_stub() -> BoschCameraCoordinator:
    coord = BoschCameraCoordinator.__new__(BoschCameraCoordinator)
    for attr in BoschCameraCoordinator._PURGE_CAM_DICT_ATTRS:
        setattr(coord, attr, {})
    for attr in BoschCameraCoordinator._PURGE_CAM_SET_ATTRS:
        setattr(coord, attr, set())
    coord._rcp_lan_denied_until = {}
    # `tls_proxy_servers` is deliberately NOT in `_PURGE_CAM_DICT_ATTRS`
    # (its value is a live asyncio.Server, closed explicitly by
    # `_purge_cam_id` instead of a bare dict.pop) — wire it up separately so
    # `_purge_cam_id`'s `cam_id in self.tls_proxy_servers` check has an
    # attribute to read. Left empty: these tests don't exercise an active
    # TLS proxy, so `_purge_cam_id` never reaches the async-close branch
    # that would otherwise need `coord.hass`/`coord.bg_tasks` stubbed too.
    coord.tls_proxy_servers = {}
    # Session-State-Facade Slice 3: `live_connections`/`user_intent_streams`
    # are no longer plain dict/set instances (folded into `_sessions`, which
    # the loop above already wired up via `_PURGE_CAM_DICT_ATTRS`) — wire the
    # same CacheFieldView/BoolFieldView facades the real coordinator uses in
    # __init__, matching this stub's pre-existing pattern for the earlier
    # slices' folded fields.
    coord.live_connections = CacheFieldView(coord._sessions, "live_connection")
    coord.user_intent_streams = BoolFieldView(coord._sessions, "user_intent_stream")
    return coord


class TestCameraRemovalRace:
    """A camera can be removed from the Bosch cloud account (deleted /
    unshared) at any moment — including while an RCP fetch or a stream
    request for that exact cam_id is still in flight.
    `cleanup_stale_devices` calls `_purge_cam_id` as soon as it notices
    the camera is gone; the in-flight operation's write-back can land
    either before or after that purge. Neither ordering may raise a
    KeyError or leave the coordinator in a state where the NEXT tick's
    purge (or any other per-cam operation) crashes."""

    async def test_purge_during_inflight_write_no_keyerror_and_self_heals(self):
        coord = _make_purge_stub()
        coord.cached_status[CAM_A] = {"status": "ONLINE"}
        coord.live_connections[CAM_A] = {"rtspsUrl": "rtsps://original"}
        coord.user_intent_streams.add(CAM_A)

        fetch_started = asyncio.Event()
        resume = asyncio.Event()

        async def inflight_rcp_fetch():
            fetch_started.set()
            await resume.wait()
            # The in-flight fetch's write-back lands AFTER the camera was
            # already purged from the account — a real race, not just a
            # theoretical one (RCP fetch + `cleanup_stale_devices` run on
            # independent schedules within the same tick).
            coord.cached_status[CAM_A] = {"status": "ONLINE", "stale": True}
            coord.live_connections[CAM_A] = {"rtspsUrl": "rtsps://resurrected"}

        task = asyncio.create_task(inflight_rcp_fetch())
        await asyncio.wait_for(fetch_started.wait(), timeout=2.0)

        # Camera vanishes from the Bosch account mid-fetch.
        BoschCameraCoordinator._purge_cam_id(coord, CAM_A)
        assert CAM_A not in coord.cached_status
        assert CAM_A not in coord.live_connections
        assert CAM_A not in coord.user_intent_streams

        resume.set()
        await asyncio.wait_for(task, timeout=2.0)
        # The stale write resurrected the cam_id — expected, the fetch
        # started before the purge and nothing can retroactively cancel it.
        assert CAM_A in coord.cached_status

        # Self-heal: the NEXT tick's `cleanup_stale_devices` purges again
        # (idempotent — no KeyError on an already-purged/re-populated dict)
        # and this time nothing resurrects it.
        BoschCameraCoordinator._purge_cam_id(coord, CAM_A)
        assert CAM_A not in coord.cached_status
        assert CAM_A not in coord.live_connections
        assert CAM_A not in coord.user_intent_streams

    async def test_purge_of_one_camera_never_touches_another(self):
        """Cross-contamination guard: purging CAM_A while a concurrent
        operation for CAM_B is in flight must leave CAM_B's state
        completely untouched."""
        coord = _make_purge_stub()
        coord.cached_status[CAM_A] = {"status": "ONLINE"}
        coord.cached_status[CAM_B] = {"status": "ONLINE"}
        coord._rcp_lan_denied_until[(CAM_A, "0x01")] = time.monotonic()
        coord._rcp_lan_denied_until[(CAM_B, "0x01")] = time.monotonic()

        concurrent_write_done = asyncio.Event()

        async def inflight_cam_b_write():
            await asyncio.sleep(0.01)
            coord.cached_status[CAM_B] = {"status": "ONLINE", "touched": True}
            concurrent_write_done.set()

        task = asyncio.create_task(inflight_cam_b_write())
        BoschCameraCoordinator._purge_cam_id(coord, CAM_A)
        await asyncio.wait_for(concurrent_write_done.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)

        assert CAM_A not in coord.cached_status
        assert (CAM_A, "0x01") not in coord._rcp_lan_denied_until
        # CAM_B is completely unaffected by CAM_A's purge.
        assert coord.cached_status[CAM_B] == {"status": "ONLINE", "touched": True}
        assert (CAM_B, "0x01") in coord._rcp_lan_denied_until

        # Double-purge (simulating the camera also vanishing next tick)
        # must not raise even though nothing is left to remove.
        BoschCameraCoordinator._purge_cam_id(coord, CAM_A)


def _smb_coord(options: dict | None = None) -> SimpleNamespace:
    opts = dict(options or {})
    opts.setdefault("enable_local_save", True)
    opts.setdefault("enable_smb_upload", True)
    opts.setdefault("smb_server", "192.168.99.99")  # deliberately fake/unreachable
    opts.setdefault("smb_share", "SHARE")
    opts.setdefault("smb_username", "user")
    opts.setdefault("smb_password", "pass")
    opts.setdefault("smb_base_path", "Bosch")
    opts.setdefault("folder_pattern", "{year}/{month}/{day}")
    opts.setdefault("file_pattern", "{camera}_{date}_{time}_{type}_{id}")
    return SimpleNamespace(options=opts, token="tok", _download_started_at=0.0)


def _fake_smb_module() -> MagicMock:
    smb = MagicMock()
    smb.register_session = MagicMock()
    smb.mkdir = MagicMock()
    smb.open_file = MagicMock()
    smb.stat = MagicMock(side_effect=OSError("missing"))
    smb.scandir = MagicMock(return_value=[])
    smb.remove = MagicMock()
    return smb


class TestSmbFtpUnreachableChaos:
    """The share is unreachable (dead NAS, wrong IP, network partition).
    `socket.setdefaulttimeout` bounds every blocking smbclient call
    (`_SMB_TRANSFER_TIMEOUT` hardening); the async caller
    (`hass.async_add_executor_job` wrapped in `asyncio.wait_for`, matching
    fcm.py's real call site) must never hang the event loop, and the
    executor + socket-timeout global state must be clean for the very
    next upload attempt."""

    def test_transfer_timeout_bounded_and_resets_socket_timeout(self) -> None:
        """Direct (sync) reproduction of an unreachable-share hang: the
        transfer loop raises TimeoutError, but `socket.setdefaulttimeout`
        must still be reset to None afterward — no leaked global state for
        the next executor job on the same thread."""
        from custom_components.bosch_shc_camera import smb
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_coord()
        fake_smb = _fake_smb_module()
        calls: list[float | None] = []

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch.object(
                smb.socket,
                "setdefaulttimeout",
                side_effect=lambda v=None: calls.append(v),
            ),
            patch.object(
                smb,
                "_sync_smb_upload_events",
                side_effect=TimeoutError("chaos: unreachable NAS share"),
            ),
        ):
            with pytest.raises(TimeoutError):
                sync_smb_upload(coord, {}, "tok")

        assert calls[-1] is None, (
            "socket.setdefaulttimeout must reset even on a hard fault"
        )
        assert smb._SMB_TRANSFER_TIMEOUT in calls

    async def test_executor_wrapped_upload_never_hangs_event_loop_and_self_heals(self):
        """The REAL async call shape (mirrors fcm.py's
        `asyncio.wait_for(hass.async_add_executor_job(sync_smb_upload, ...),
        timeout=30.0)` — here scaled down): an unreachable share must not
        block the event loop past the outer bound, and the very next
        upload attempt right after (same executor, same coordinator) must
        succeed normally."""
        from custom_components.bosch_shc_camera import smb
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_coord()
        fake_smb = _fake_smb_module()

        def _hung_share(*_a, **_kw):
            time.sleep(0.05)
            raise TimeoutError("chaos: unreachable NAS share (executor thread)")

        loop = asyncio.get_running_loop()
        hass_stub = SimpleNamespace(
            async_add_executor_job=lambda func, *args: loop.run_in_executor(
                None, func, *args
            )
        )

        start = time.monotonic()
        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch.object(smb, "_sync_smb_upload_events", side_effect=_hung_share),
        ):
            try:
                await asyncio.wait_for(
                    hass_stub.async_add_executor_job(
                        sync_smb_upload, coord, {}, coord.token
                    ),
                    timeout=2.0,
                )
            except TimeoutError:
                pass  # matches fcm.py's own `except TimeoutError:` handling
        elapsed = time.monotonic() - start
        assert elapsed < 2.5, "outer wait_for must bound the call — no event-loop hang"

        # Self-heal: a subsequent, healthy upload right after (empty event
        # list — trivially succeeds) must complete cleanly. Proves the
        # failed executor-thread call didn't wedge the shared
        # socket.setdefaulttimeout global state or the executor itself.
        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            await asyncio.wait_for(
                hass_stub.async_add_executor_job(
                    sync_smb_upload, coord, {}, coord.token
                ),
                timeout=2.0,
            )
