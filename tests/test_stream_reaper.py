"""Regression tests for the idle-session reaper (v13.5.6).

Background: a LOCAL session (card view, Cast, camera.play_stream, camera.record,
media-browser preview) keeps the camera's RTSP session alive through the
keepalive loop. When the consumer goes away — tab closed, navigated away, Cast
stopped — nothing ends it, so the session lingered until the maxSessionDuration
recycle (effectively forever) — a ghost that needlessly burns Bosch's 60-min
LOCAL session cap.

`_idle_session_reaper` tears such a session down once there has been no consumer
for `STREAM_IDLE_REAP_SEC`. Reaping is driven by consumer presence, NOT the
switch state (Weg 2): an active viewer (HLS/WebRTC) or a Mini-NVR recorder counts
as a consumer and is never reaped, so automations that use the stream are
unaffected; a switch left ON that nobody is watching is itself the ghost.

These tests pin, with FAKE cam IDs only (never real device values):
  * reaper tears down after the grace window (no consumer)
  * reaper reaps regardless of switch state (switch ON + no consumer → reaped)
  * reaper does not reap while a consumer is present
  * the idle timer RESETS when a consumer returns (no premature reap)
  * the loop exits cleanly on stale generation / session gone
  * `_has_active_consumer` honours an active NVR recorder, HLS
    (`stream.available`), and go2rtc consumers
  * `_go2rtc_consumer_count` parses consumers, tries both ports, returns None
    when go2rtc is unreachable
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.const import STREAM_IDLE_REAP_SEC

CAM = "11111111-1111-1111-1111-111111111111"
GEN = 1
MOD = "custom_components.bosch_shc_camera"


class _Stop(Exception):
    """Sentinel raised from the patched sleep to bound otherwise-infinite loops."""


def _bounded_sleep(max_iters: int, on_iter=None):
    """Patched asyncio.sleep that raises _Stop after `max_iters` ticks.

    sleep sits at the TOP of the reaper loop, so raising on tick N+1 stops the
    loop cleanly after exactly N full iterations. `on_iter(i)` runs each tick
    (e.g. to mutate generation mid-run).
    """
    state = {"i": 0}

    async def _sleep(_delay):
        state["i"] += 1
        if on_iter is not None:
            on_iter(state["i"])
        if state["i"] > max_iters:
            raise _Stop

    return _sleep


def _reaper_coord(**overrides) -> SimpleNamespace:
    base = dict(
        _session_idle_since={},
        _auto_renew_generation={CAM: GEN},
        _live_connections={CAM: {"_connection_type": "LOCAL"}},
        _user_intent_streams=set(),
        _has_active_consumer=AsyncMock(return_value=False),
        _tear_down_live_stream=MagicMock(return_value="td-coro"),
        hass=SimpleNamespace(async_create_task=MagicMock()),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── _idle_session_reaper ─────────────────────────────────────────────────────


class TestIdleSessionReaper:
    @pytest.mark.asyncio
    async def test_reaps_switch_off_no_consumer_after_grace(self):
        """Switch OFF + no consumer for >= STREAM_IDLE_REAP_SEC → teardown
        scheduled exactly once, in its own task (not awaited inline)."""
        c = _reaper_coord()
        t0 = 1000.0
        monotonic = MagicMock(side_effect=[t0, t0 + STREAM_IDLE_REAP_SEC + 1])
        with (
            patch("custom_components.bosch_shc_camera.asyncio.sleep", AsyncMock()),
            patch("custom_components.bosch_shc_camera.time.monotonic", monotonic),
        ):
            await BoschCameraCoordinator._idle_session_reaper(c, CAM, GEN)
        c._tear_down_live_stream.assert_called_once_with(CAM, expected_generation=GEN)
        c.hass.async_create_task.assert_called_once()
        assert CAM not in c._session_idle_since  # cleared on the way out

    @pytest.mark.asyncio
    async def test_first_tick_only_arms_timer_no_reap(self):
        """A single no-consumer tick only arms the timer — it must NOT reap."""
        c = _reaper_coord()
        monotonic = MagicMock(return_value=1000.0)
        with (
            patch(
                "custom_components.bosch_shc_camera.asyncio.sleep",
                _bounded_sleep(1),
            ),
            patch("custom_components.bosch_shc_camera.time.monotonic", monotonic),
            pytest.raises(_Stop),
        ):
            await BoschCameraCoordinator._idle_session_reaper(c, CAM, GEN)
        c._tear_down_live_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaps_regardless_of_switch_state(self):
        """Weg 2: reaping is driven by consumer presence, not the switch. A
        session with the switch ON but no consumer (e.g. user tapped play then
        navigated away, leaving the switch on) is itself the ghost → reaped."""
        c = _reaper_coord(_user_intent_streams={CAM})  # switch ON
        t0 = 1000.0
        monotonic = MagicMock(side_effect=[t0, t0 + STREAM_IDLE_REAP_SEC + 1])
        with (
            patch("custom_components.bosch_shc_camera.asyncio.sleep", AsyncMock()),
            patch("custom_components.bosch_shc_camera.time.monotonic", monotonic),
        ):
            await BoschCameraCoordinator._idle_session_reaper(c, CAM, GEN)
        c._tear_down_live_stream.assert_called_once_with(CAM, expected_generation=GEN)

    @pytest.mark.asyncio
    async def test_does_not_reap_while_consumer_present(self):
        """An active consumer keeps the session alive indefinitely."""
        c = _reaper_coord(_has_active_consumer=AsyncMock(return_value=True))
        with (
            patch(
                "custom_components.bosch_shc_camera.asyncio.sleep",
                _bounded_sleep(5),
            ),
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                MagicMock(return_value=0.0),
            ),
            pytest.raises(_Stop),
        ):
            await BoschCameraCoordinator._idle_session_reaper(c, CAM, GEN)
        c._tear_down_live_stream.assert_not_called()
        assert c._has_active_consumer.await_count >= 1
        assert CAM not in c._session_idle_since

    @pytest.mark.asyncio
    async def test_idle_timer_resets_when_consumer_returns(self):
        """absent → absent → present → absent must NOT reap, even though the
        total elapsed exceeds the grace: the timer resets on consumer return."""
        # absent, absent, present, absent
        c = _reaper_coord(
            _has_active_consumer=AsyncMock(side_effect=[False, False, True, False])
        )
        # monotonic is called once per ABSENT tick: ticks 1,2,4 → 3 values.
        # tick1 arms at 1000; tick2 at 1050 (<grace, no reap); tick4 re-arms at
        # 9000 (fresh, well past 1000+grace but timer was reset at tick3).
        monotonic = MagicMock(side_effect=[1000.0, 1050.0, 9000.0])
        with (
            patch(
                "custom_components.bosch_shc_camera.asyncio.sleep",
                _bounded_sleep(4),
            ),
            patch("custom_components.bosch_shc_camera.time.monotonic", monotonic),
            pytest.raises(_Stop),
        ):
            await BoschCameraCoordinator._idle_session_reaper(c, CAM, GEN)
        # No reap: had the timer NOT reset when the consumer returned at tick3,
        # tick4 would have seen elapsed = 9000-1000 = 8000s >= grace and reaped.
        c._tear_down_live_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_exits_on_stale_generation(self):
        """A newer session (OFF→ON / renewal) bumps the generation → exit."""
        c = _reaper_coord(_auto_renew_generation={CAM: GEN + 1})
        with (
            patch("custom_components.bosch_shc_camera.asyncio.sleep", AsyncMock()),
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                MagicMock(return_value=0.0),
            ),
        ):
            await BoschCameraCoordinator._idle_session_reaper(c, CAM, GEN)
        c._tear_down_live_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_exits_when_session_gone(self):
        """Session already torn down / no longer LOCAL → exit without reaping."""
        c = _reaper_coord(_live_connections={})
        with (
            patch("custom_components.bosch_shc_camera.asyncio.sleep", AsyncMock()),
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                MagicMock(return_value=0.0),
            ),
        ):
            await BoschCameraCoordinator._idle_session_reaper(c, CAM, GEN)
        c._tear_down_live_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_exits_when_connection_no_longer_local(self):
        """A LOCAL→REMOTE fallback leaves a REMOTE session the reaper ignores."""
        c = _reaper_coord(_live_connections={CAM: {"_connection_type": "REMOTE"}})
        with (
            patch("custom_components.bosch_shc_camera.asyncio.sleep", AsyncMock()),
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                MagicMock(return_value=0.0),
            ),
        ):
            await BoschCameraCoordinator._idle_session_reaper(c, CAM, GEN)
        c._tear_down_live_stream.assert_not_called()


# ── _replace_reaper_task ─────────────────────────────────────────────────────


class TestReplaceReaperTask:
    @pytest.mark.asyncio
    async def test_cancels_old_and_tracks_new(self):
        """Replacing the reaper cancels a running one, registers the new task in
        both _reaper_tasks and _bg_tasks, and wires the done-callback."""
        old = MagicMock()
        old.done.return_value = False
        new = MagicMock()
        coord = SimpleNamespace(
            _reaper_tasks={CAM: old},
            _bg_tasks=set(),
            hass=SimpleNamespace(
                async_create_background_task=MagicMock(return_value=new)
            ),
        )

        async def _coro():  # pragma: no cover — never awaited (mocked task)
            return None

        c = _coro()
        out = BoschCameraCoordinator._replace_reaper_task(coord, CAM, c)
        c.close()  # avoid "coroutine never awaited" warning
        old.cancel.assert_called_once()
        assert out is new
        assert coord._reaper_tasks[CAM] is new
        assert new in coord._bg_tasks
        new.add_done_callback.assert_called_once_with(coord._bg_tasks.discard)


# ── _has_active_consumer ─────────────────────────────────────────────────────


class TestHasActiveConsumer:
    def _coord(self, *, token, go2rtc_count, nvr=False):
        stream = SimpleNamespace(access_token=token) if token else None
        cam_entity = SimpleNamespace(stream=stream, entity_id="camera.bosch_test")
        return SimpleNamespace(
            _camera_entities={CAM: cam_entity},
            _nvr_processes={CAM: object()} if nvr else {},
            _go2rtc_consumer_count=AsyncMock(return_value=go2rtc_count),
            _frigate_runner=None,
        )

    @pytest.mark.asyncio
    async def test_true_when_nvr_recording_without_polling_anything(self):
        """An active Mini-NVR recorder reads the proxy directly — it must count
        as a consumer so the reaper never kills a running recording."""
        c = self._coord(token=None, go2rtc_count=0, nvr=True)
        with patch(f"{MOD}.cf_unbuffer.hls_access_age") as age:
            assert await BoschCameraCoordinator._has_active_consumer(c, CAM) is True
            age.assert_not_called()  # NVR short-circuits before HLS
        c._go2rtc_consumer_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_true_when_hls_fetched_recently(self):
        """A playlist/segment fetched within the freshness window = live viewer."""
        c = self._coord(token="tok123", go2rtc_count=0)
        with patch(f"{MOD}.cf_unbuffer.hls_access_age", return_value=5.0):
            assert await BoschCameraCoordinator._has_active_consumer(c, CAM) is True
        c._go2rtc_consumer_count.assert_not_awaited()  # HLS short-circuits

    @pytest.mark.asyncio
    async def test_false_when_hls_stale(self):
        """Regression: HLS last fetched long ago (viewer gone) is NOT a consumer.
        HA's Stream.available would have stayed True here and pinned the ghost."""
        c = self._coord(token="tok123", go2rtc_count=0)
        with patch(f"{MOD}.cf_unbuffer.hls_access_age", return_value=999.0):
            assert await BoschCameraCoordinator._has_active_consumer(c, CAM) is False

    @pytest.mark.asyncio
    async def test_false_when_hls_never_fetched(self):
        c = self._coord(token="tok123", go2rtc_count=0)
        with patch(f"{MOD}.cf_unbuffer.hls_access_age", return_value=None):
            assert await BoschCameraCoordinator._has_active_consumer(c, CAM) is False

    @pytest.mark.asyncio
    async def test_true_when_go2rtc_has_consumers(self):
        c = self._coord(token=None, go2rtc_count=2)
        with patch(f"{MOD}.cf_unbuffer.hls_access_age", return_value=None):
            assert await BoschCameraCoordinator._has_active_consumer(c, CAM) is True

    @pytest.mark.asyncio
    async def test_true_when_go2rtc_unreachable_unknown_keeps_alive(self):
        # go2rtc unreachable (None) means we CANNOT confirm the session is idle.
        # Treating unknown as "no consumer" used to reap LIVE WebRTC viewers on
        # setups where go2rtc answers on a non-default port (consumer invisible
        # to us) — the stream "just died" every grace window. Unknown ⇒ keep
        # alive; only a confirmed 0 may reap. Regression: 2026-06-03 reaper fix.
        c = self._coord(token=None, go2rtc_count=None)
        with patch(f"{MOD}.cf_unbuffer.hls_access_age", return_value=None):
            assert await BoschCameraCoordinator._has_active_consumer(c, CAM) is True

    @pytest.mark.asyncio
    async def test_false_when_go2rtc_confirms_zero_consumers(self):
        # A CONFIRMED 0 (go2rtc reachable, registered, but nobody reading) is the
        # only consumer signal that permits reaping — the genuine ghost session.
        c = self._coord(token=None, go2rtc_count=0)
        with patch(f"{MOD}.cf_unbuffer.hls_access_age", return_value=None):
            assert await BoschCameraCoordinator._has_active_consumer(c, CAM) is False

    @pytest.mark.asyncio
    async def test_no_camera_entity_falls_back_to_go2rtc(self):
        c = SimpleNamespace(
            _camera_entities={},
            _nvr_processes={},
            _go2rtc_consumer_count=AsyncMock(return_value=1),
            _frigate_runner=None,
        )
        assert await BoschCameraCoordinator._has_active_consumer(c, CAM) is True


# ── _go2rtc_consumer_count ───────────────────────────────────────────────────


def _session_with_get(get_handler):
    session = MagicMock()
    session.get = get_handler
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestGo2rtcConsumerCount:
    def _coord(self):
        return SimpleNamespace(
            _camera_entities={CAM: SimpleNamespace(entity_id="camera.bosch_test")}
        )

    @pytest.mark.asyncio
    async def test_counts_consumers_list(self):
        @asynccontextmanager
        async def _get(*a, **kw):
            yield SimpleNamespace(
                status=200,
                json=AsyncMock(return_value={"consumers": [{}, {}, {}]}),
            )

        with patch("aiohttp.ClientSession", return_value=_session_with_get(_get)):
            n = await BoschCameraCoordinator._go2rtc_consumer_count(self._coord(), CAM)
        assert n == 3

    @pytest.mark.asyncio
    async def test_registered_but_no_consumers_returns_zero(self):
        @asynccontextmanager
        async def _get(*a, **kw):
            yield SimpleNamespace(status=200, json=AsyncMock(return_value={}))

        with patch("aiohttp.ClientSession", return_value=_session_with_get(_get)):
            n = await BoschCameraCoordinator._go2rtc_consumer_count(self._coord(), CAM)
        assert n == 0

    @pytest.mark.asyncio
    async def test_first_port_non_200_falls_through_to_second(self):
        calls = {"n": 0}

        @asynccontextmanager
        async def _get(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                yield SimpleNamespace(status=404, json=AsyncMock(return_value={}))
            else:
                yield SimpleNamespace(
                    status=200, json=AsyncMock(return_value={"consumers": [{}]})
                )

        with patch("aiohttp.ClientSession", return_value=_session_with_get(_get)):
            n = await BoschCameraCoordinator._go2rtc_consumer_count(self._coord(), CAM)
        assert n == 1
        assert calls["n"] == 2  # second endpoint was tried

    @pytest.mark.asyncio
    async def test_returns_none_when_unreachable(self):
        import aiohttp

        @asynccontextmanager
        async def _get(*a, **kw):
            raise aiohttp.ClientError("connection refused")
            yield  # pragma: no cover

        with patch("aiohttp.ClientSession", return_value=_session_with_get(_get)):
            n = await BoschCameraCoordinator._go2rtc_consumer_count(self._coord(), CAM)
        assert n is None

    @pytest.mark.asyncio
    async def test_uses_legacy_name_without_camera_entity(self):
        captured = {}

        @asynccontextmanager
        async def _get(url, **kw):
            captured["params"] = kw.get("params", {})
            yield SimpleNamespace(
                status=200, json=AsyncMock(return_value={"consumers": []})
            )

        coord = SimpleNamespace(_camera_entities={})
        with patch("aiohttp.ClientSession", return_value=_session_with_get(_get)):
            n = await BoschCameraCoordinator._go2rtc_consumer_count(coord, CAM)
        assert n == 0
        assert captured["params"]["src"] == f"bosch_shc_cam_{CAM.lower()}"
