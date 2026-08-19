"""Tests for slow_tier.py — CamContext/_compute_cam_context (Phase 2
step 7 sub-step 1), _poll_cam_info_caches (sub-step 2),
_poll_cam_control (sub-step 3), and _poll_slow_tier_endpoints
(sub-step 4) of the coordinator rewrite. Direct unit tests in
isolation; the existing integration-level tests exercising the full
_async_update_data (test_init.py) already cover end-to-end wiring."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera.slow_tier import (
    CamContext,
    _compute_cam_context,
    _poll_cam_control,
    _poll_cam_info_caches,
    _poll_slow_tier_endpoints,
)

CAM_A = "11111111-1111-1111-1111-111111111111"
NOW = 1000.0


def _make_coord(**overrides):
    coord = SimpleNamespace(
        live_connections=overrides.pop("live_connections", {}),
        slow_tier_deferred=overrides.pop("slow_tier_deferred", set()),
        slow_tier_defer_since=overrides.pop("slow_tier_defer_since", {}),
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _cam_raw(**overrides):
    raw = {
        "hardwareVersion": overrides.pop("hardwareVersion", "CAMERA"),
        "featureSupport": overrides.pop("featureSupport", {}),
        "privacyMode": overrides.pop("privacyMode", ""),
    }
    raw.update(overrides)
    return raw


class TestOnlineStatus:
    def test_online_when_status_online(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert ctx.is_online is True

    def test_offline_when_status_not_online(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "OFFLINE"}}, {}, False
        )
        assert ctx.is_online is False

    def test_unknown_status_defaults_offline(self):
        coord = _make_coord()
        ctx = _compute_cam_context(coord, CAM_A, _cam_raw(), {CAM_A: {}}, {}, False)
        assert ctx.is_online is False


class TestHwAndGeneration:
    def test_gen1_hardware(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord,
            CAM_A,
            _cam_raw(hardwareVersion="CAMERA"),
            {CAM_A: {"status": "ONLINE"}},
            {},
            False,
        )
        assert ctx.is_gen2 is False
        assert ctx.hw == "CAMERA"

    def test_gen2_hardware(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord,
            CAM_A,
            _cam_raw(hardwareVersion="HOME_Eyes_Outdoor"),
            {CAM_A: {"status": "ONLINE"}},
            {},
            False,
        )
        assert ctx.is_gen2 is True


class TestPanAndLight:
    def test_pan_limit_and_has_light_read_from_feature_support(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord,
            CAM_A,
            _cam_raw(featureSupport={"panLimit": 270, "light": True}),
            {CAM_A: {"status": "ONLINE"}},
            {},
            False,
        )
        assert ctx.pan_limit == 270
        assert ctx.has_light is True

    def test_missing_feature_support_defaults(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert ctx.pan_limit == 0
        assert ctx.has_light is False


class TestPrivacyAndStreamType:
    def test_privacy_on_detected(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord,
            CAM_A,
            _cam_raw(privacyMode="ON"),
            {CAM_A: {"status": "ONLINE"}},
            {},
            False,
        )
        assert ctx.privacy_on is True

    def test_privacy_off_by_default(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert ctx.privacy_on is False

    def test_local_stream_active_true_for_local_connection(self):
        coord = _make_coord(live_connections={CAM_A: {"_connection_type": "LOCAL"}})
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert ctx.local_stream_active is True
        assert ctx.stream_active is True

    def test_local_stream_active_false_for_remote_connection(self):
        coord = _make_coord(live_connections={CAM_A: {"_connection_type": "REMOTE"}})
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert ctx.local_stream_active is False
        assert ctx.stream_active is True  # a connection exists, just not LOCAL

    def test_no_connection_stream_inactive(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert ctx.stream_active is False
        assert ctx.local_stream_active is False


class TestDeferGate:
    def test_do_slow_cam_true_on_normal_interval_no_stream(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, True
        )
        assert ctx.do_slow_cam is True

    def test_defers_when_stream_active_and_due(self):
        coord = _make_coord(live_connections={CAM_A: {"_connection_type": "LOCAL"}})
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, True
        )
        assert ctx.do_slow_cam is False
        assert CAM_A in coord.slow_tier_deferred
        assert CAM_A in coord.slow_tier_defer_since

    def test_defer_disabled_via_option_runs_regardless_of_stream(self):
        coord = _make_coord(live_connections={CAM_A: {"_connection_type": "LOCAL"}})
        ctx = _compute_cam_context(
            coord,
            CAM_A,
            _cam_raw(),
            {CAM_A: {"status": "ONLINE"}},
            {"defer_diag_during_stream": False},
            True,
        )
        assert ctx.do_slow_cam is True
        assert CAM_A not in coord.slow_tier_deferred

    def test_previously_deferred_runs_once_stream_goes_idle(self):
        coord = _make_coord(
            slow_tier_deferred={CAM_A},
            slow_tier_defer_since={CAM_A: time.monotonic()},
        )
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert ctx.do_slow_cam is True
        assert CAM_A not in coord.slow_tier_deferred
        assert CAM_A not in coord.slow_tier_defer_since

    def test_defer_bound_forces_read_despite_active_stream(self):
        coord = _make_coord(
            live_connections={CAM_A: {"_connection_type": "LOCAL"}},
            slow_tier_deferred={CAM_A},
            slow_tier_defer_since={CAM_A: time.monotonic() - 3600.0},
        )
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, True
        )
        assert ctx.do_slow_cam is True
        assert CAM_A not in coord.slow_tier_deferred

    def test_do_slow_cam_false_when_not_due_and_no_stream(self):
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert ctx.do_slow_cam is False

    def test_skip_log_fires_when_due_but_offline(self):
        """Branch coverage: do_slow_cam True + camera offline logs the
        skip message instead of silently no-op-ing."""
        coord = _make_coord()
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "OFFLINE"}}, {}, True
        )
        assert ctx.do_slow_cam is True
        assert ctx.is_online is False


class TestStubCoordinatorLazyInit:
    def test_missing_deferred_attrs_lazily_initialized(self):
        """Stub coordinators in other unit tests bypass __init__ and may
        lack slow_tier_deferred/slow_tier_defer_since entirely — the
        hasattr-based lazy-init must tolerate this without crashing."""
        coord = SimpleNamespace(live_connections={})
        ctx = _compute_cam_context(
            coord, CAM_A, _cam_raw(), {CAM_A: {"status": "ONLINE"}}, {}, False
        )
        assert isinstance(ctx, CamContext)
        assert coord.slow_tier_deferred == set()
        assert coord.slow_tier_defer_since == {}


def _make_info_coord(**overrides):
    coord = SimpleNamespace(
        shc_state_cache=overrides.pop("shc_state_cache", {}),
        privacy_set_at=overrides.pop("privacy_set_at", {}),
        light_set_at=overrides.pop("light_set_at", {}),
        notif_set_at=overrides.pop("notif_set_at", {}),
        WRITE_LOCK_SECS=overrides.pop("WRITE_LOCK_SECS", 10.0),
        live_connections=overrides.pop("live_connections", {}),
        lighting_switch_cache=overrides.pop("lighting_switch_cache", {}),
        hass=MagicMock(),
        tear_down_live_stream=MagicMock(return_value="coro"),
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


class TestPollCamInfoCachesPrivacy:
    def test_privacy_on_written(self):
        coord = _make_info_coord()
        _poll_cam_info_caches(coord, CAM_A, {"privacyMode": "ON"})
        assert coord.shc_state_cache[CAM_A]["privacy_mode"] is True

    def test_privacy_off_written(self):
        coord = _make_info_coord()
        _poll_cam_info_caches(coord, CAM_A, {"privacyMode": "OFF"})
        assert coord.shc_state_cache[CAM_A]["privacy_mode"] is False

    def test_privacy_locked_skips_overwrite(self):
        coord = _make_info_coord(privacy_set_at={CAM_A: time.monotonic()})
        coord.shc_state_cache[CAM_A] = {"privacy_mode": False}
        _poll_cam_info_caches(coord, CAM_A, {"privacyMode": "ON"})
        assert coord.shc_state_cache[CAM_A]["privacy_mode"] is False

    def test_external_privacy_on_transition_tears_down_active_stream(self):
        coord = _make_info_coord(
            live_connections={CAM_A: {"_connection_type": "LOCAL"}}
        )
        coord.shc_state_cache[CAM_A] = {"privacy_mode": False}
        _poll_cam_info_caches(coord, CAM_A, {"privacyMode": "ON"})
        coord.hass.async_create_task.assert_called_once()
        coord.tear_down_live_stream.assert_called_once_with(CAM_A)

    def test_privacy_already_on_no_teardown(self):
        """No OFF→ON transition (already True) must not re-trigger teardown."""
        coord = _make_info_coord(
            live_connections={CAM_A: {"_connection_type": "LOCAL"}}
        )
        coord.shc_state_cache[CAM_A] = {"privacy_mode": True}
        _poll_cam_info_caches(coord, CAM_A, {"privacyMode": "ON"})
        coord.hass.async_create_task.assert_not_called()

    def test_privacy_on_no_active_stream_no_teardown(self):
        coord = _make_info_coord()
        coord.shc_state_cache[CAM_A] = {"privacy_mode": False}
        _poll_cam_info_caches(coord, CAM_A, {"privacyMode": "ON"})
        coord.hass.async_create_task.assert_not_called()

    def test_empty_privacy_str_skips_entirely(self):
        coord = _make_info_coord()
        coord.shc_state_cache[CAM_A] = {"privacy_mode": True}
        _poll_cam_info_caches(coord, CAM_A, {})
        assert coord.shc_state_cache[CAM_A]["privacy_mode"] is True


class TestPollCamInfoCachesLight:
    def test_gen1_light_from_feature_status(self):
        coord = _make_info_coord()
        _poll_cam_info_caches(
            coord,
            CAM_A,
            {
                "hardwareVersion": "CAMERA",
                "featureStatus": {
                    "frontIlluminatorInGeneralLightOn": True,
                    "wallwasherInGeneralLightOn": False,
                    "frontIlluminatorGeneralLightIntensity": 0.7,
                },
            },
        )
        cache = coord.shc_state_cache[CAM_A]
        assert cache["camera_light"] is True
        assert cache["front_light"] is True
        assert cache["wallwasher"] is False
        assert cache["front_light_intensity"] == 0.7

    def test_gen1_intensity_none_not_written(self):
        """Branch coverage: intensity absent from featureStatus must
        leave front_light_intensity at its prior cached value."""
        coord = _make_info_coord()
        coord.shc_state_cache[CAM_A] = {"front_light_intensity": 0.3}
        _poll_cam_info_caches(
            coord,
            CAM_A,
            {
                "hardwareVersion": "CAMERA",
                "featureStatus": {"frontIlluminatorInGeneralLightOn": True},
            },
        )
        assert coord.shc_state_cache[CAM_A]["front_light_intensity"] == 0.3

    def test_gen2_light_from_lighting_switch_cache(self):
        coord = _make_info_coord(
            lighting_switch_cache={
                CAM_A: {
                    "frontLightSettings": {"brightness": 50},
                    "topLedLightSettings": {"brightness": 0},
                    "bottomLedLightSettings": {"brightness": 0},
                }
            }
        )
        _poll_cam_info_caches(
            coord,
            CAM_A,
            {
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "featureStatus": {"frontIlluminatorInGeneralLightOn": True},
            },
        )
        cache = coord.shc_state_cache[CAM_A]
        assert cache["camera_light"] is True
        assert cache["front_light"] is True
        assert cache["front_light_intensity"] == 0.5

    def test_gen2_no_lighting_switch_cache_yet_keeps_current_values(self):
        coord = _make_info_coord()
        coord.shc_state_cache[CAM_A] = {"camera_light": True, "front_light": True}
        _poll_cam_info_caches(
            coord,
            CAM_A,
            {
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "featureStatus": {"frontIlluminatorInGeneralLightOn": True},
            },
        )
        cache = coord.shc_state_cache[CAM_A]
        assert cache["camera_light"] is True
        assert cache["front_light"] is True

    def test_light_locked_skips_overwrite(self):
        coord = _make_info_coord(light_set_at={CAM_A: time.monotonic()})
        coord.shc_state_cache[CAM_A] = {"camera_light": True}
        _poll_cam_info_caches(
            coord,
            CAM_A,
            {
                "hardwareVersion": "CAMERA",
                "featureStatus": {"frontIlluminatorInGeneralLightOn": False},
            },
        )
        assert coord.shc_state_cache[CAM_A]["camera_light"] is True

    def test_light_on_none_and_cache_unset_defaults_none(self):
        coord = _make_info_coord()
        _poll_cam_info_caches(coord, CAM_A, {"featureStatus": {}})
        assert coord.shc_state_cache[CAM_A]["camera_light"] is None

    def test_light_on_none_preserves_existing_cached_value(self):
        coord = _make_info_coord()
        coord.shc_state_cache[CAM_A] = {"camera_light": True}
        _poll_cam_info_caches(coord, CAM_A, {"featureStatus": {}})
        assert coord.shc_state_cache[CAM_A]["camera_light"] is True


class TestPollCamInfoCachesNotifications:
    def test_notifications_status_written(self):
        coord = _make_info_coord()
        _poll_cam_info_caches(coord, CAM_A, {"notificationsEnabledStatus": "ENABLED"})
        assert coord.shc_state_cache[CAM_A]["notifications_status"] == "ENABLED"

    def test_notifications_locked_skips_overwrite(self):
        coord = _make_info_coord(notif_set_at={CAM_A: time.monotonic()})
        coord.shc_state_cache[CAM_A] = {"notifications_status": "DISABLED"}
        _poll_cam_info_caches(coord, CAM_A, {"notificationsEnabledStatus": "ENABLED"})
        assert coord.shc_state_cache[CAM_A]["notifications_status"] == "DISABLED"

    def test_empty_notifications_status_skipped(self):
        coord = _make_info_coord()
        coord.shc_state_cache[CAM_A] = {"notifications_status": "ENABLED"}
        _poll_cam_info_caches(coord, CAM_A, {})
        assert coord.shc_state_cache[CAM_A]["notifications_status"] == "ENABLED"


class TestPollCamInfoCachesHasLight:
    def test_has_light_written_from_feature_support(self):
        coord = _make_info_coord()
        _poll_cam_info_caches(coord, CAM_A, {"featureSupport": {"light": True}})
        assert coord.shc_state_cache[CAM_A]["has_light"] is True

    def test_has_light_defaults_false(self):
        coord = _make_info_coord()
        _poll_cam_info_caches(coord, CAM_A, {})
        assert coord.shc_state_cache[CAM_A]["has_light"] is False


HEADERS = {"Authorization": "Bearer tok", "Accept": "application/json"}


def _make_resp(status: int, json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session(url_responses: dict):
    def _get(url, **kwargs):
        for key, resp in url_responses.items():
            if key in str(url):
                return resp
        return _make_resp(404)

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _make_control_coord(**overrides):
    coord = SimpleNamespace(
        pan_cache=overrides.pop("pan_cache", {}),
        lighting_switch_cache=overrides.pop("lighting_switch_cache", {}),
        light_set_at=overrides.pop("light_set_at", {}),
        WRITE_LOCK_SECS=overrides.pop("WRITE_LOCK_SECS", 30.0),
        shc_state_cache=overrides.pop("shc_state_cache", {}),
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _ctx(**overrides):
    defaults = dict(
        hw="CAMERA",
        is_gen2=False,
        is_online=True,
        stream_active=False,
        local_stream_active=False,
        privacy_on=False,
        do_slow_cam=False,
        pan_limit=0,
        has_light=False,
    )
    defaults.update(overrides)
    return CamContext(**defaults)


class TestPollCamControlPan:
    @pytest.mark.asyncio
    async def test_pan_fetched_when_supported_and_online(self):
        coord = _make_control_coord()
        session = _make_session(
            {"/pan": _make_resp(200, {"currentAbsolutePosition": 42})}
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(pan_limit=180, is_online=True), session, HEADERS
        )
        assert coord.pan_cache[CAM_A] == 42

    @pytest.mark.asyncio
    async def test_pan_skipped_when_no_pan_limit(self):
        coord = _make_control_coord()
        session = _make_session(
            {"/pan": _make_resp(200, {"currentAbsolutePosition": 42})}
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(pan_limit=0, is_online=True), session, HEADERS
        )
        assert CAM_A not in coord.pan_cache

    @pytest.mark.asyncio
    async def test_pan_skipped_when_offline(self):
        coord = _make_control_coord()
        session = _make_session(
            {"/pan": _make_resp(200, {"currentAbsolutePosition": 42})}
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(pan_limit=180, is_online=False), session, HEADERS
        )
        assert CAM_A not in coord.pan_cache

    @pytest.mark.asyncio
    async def test_pan_non_200_not_cached(self):
        coord = _make_control_coord()
        session = _make_session({"/pan": _make_resp(500)})
        await _poll_cam_control(
            coord, CAM_A, _ctx(pan_limit=180, is_online=True), session, HEADERS
        )
        assert CAM_A not in coord.pan_cache

    @pytest.mark.asyncio
    async def test_pan_fetch_exception_swallowed(self):
        coord = _make_control_coord()

        def _raise(*_a, **_k):
            raise RuntimeError("boom")

        session = MagicMock()
        session.get = MagicMock(side_effect=_raise)
        await _poll_cam_control(
            coord, CAM_A, _ctx(pan_limit=180, is_online=True), session, HEADERS
        )
        assert CAM_A not in coord.pan_cache


class TestPollCamControlGen2Lighting:
    @pytest.mark.asyncio
    async def test_lighting_switch_fetched_for_gen2_online(self):
        coord = _make_control_coord()
        session = _make_session(
            {"/lighting/switch": _make_resp(200, {"frontLightSettings": {}})}
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(is_gen2=True, is_online=True), session, HEADERS
        )
        assert coord.lighting_switch_cache[CAM_A] == {"frontLightSettings": {}}

    @pytest.mark.asyncio
    async def test_lighting_switch_skipped_for_gen1(self):
        coord = _make_control_coord()
        session = _make_session(
            {"/lighting/switch": _make_resp(200, {"frontLightSettings": {}})}
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(is_gen2=False, is_online=True), session, HEADERS
        )
        assert CAM_A not in coord.lighting_switch_cache

    @pytest.mark.asyncio
    async def test_lighting_switch_skipped_when_offline(self):
        coord = _make_control_coord()
        session = _make_session(
            {"/lighting/switch": _make_resp(200, {"frontLightSettings": {}})}
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(is_gen2=True, is_online=False), session, HEADERS
        )
        assert CAM_A not in coord.lighting_switch_cache

    @pytest.mark.asyncio
    async def test_lighting_switch_non_200_not_cached(self):
        coord = _make_control_coord()
        session = _make_session({"/lighting/switch": _make_resp(500)})
        await _poll_cam_control(
            coord, CAM_A, _ctx(is_gen2=True, is_online=True), session, HEADERS
        )
        assert CAM_A not in coord.lighting_switch_cache

    @pytest.mark.asyncio
    async def test_lighting_switch_exception_swallowed(self):
        coord = _make_control_coord()

        def _raise(*_a, **_k):
            raise RuntimeError("boom")

        session = MagicMock()
        session.get = MagicMock(side_effect=_raise)
        await _poll_cam_control(
            coord, CAM_A, _ctx(is_gen2=True, is_online=True), session, HEADERS
        )
        assert CAM_A not in coord.lighting_switch_cache

    @pytest.mark.asyncio
    async def test_lighting_switch_skipped_while_light_write_locked(self):
        """GitHub #66: this wholesale-overwrite poll must NOT run while a
        fresh switch/light-entity write is still inside its
        eventual-consistency window — else it immediately restores the
        cloud's still-stale brightness right after an optimistic OFF-zero,
        making the Front/Top/Bottom Light entities flip back "on" while the
        camera_light switch (protected by the same lock, elsewhere) stays
        correctly "off"."""
        coord = _make_control_coord(
            lighting_switch_cache={CAM_A: {"frontLightSettings": {"brightness": 0}}},
            light_set_at={CAM_A: time.monotonic()},
            shc_state_cache={CAM_A: {"camera_light": False}},
        )
        session = _make_session(
            {
                "/lighting/switch": _make_resp(
                    200, {"frontLightSettings": {"brightness": 80}}
                )
            }
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(is_gen2=True, is_online=True), session, HEADERS
        )
        assert coord.lighting_switch_cache[CAM_A] == {
            "frontLightSettings": {"brightness": 0}
        }, (
            "poll must not overwrite lighting_switch_cache while light_set_at lock is active"
        )

    @pytest.mark.asyncio
    async def test_lighting_switch_fetched_once_lock_expires(self):
        coord = _make_control_coord(
            lighting_switch_cache={CAM_A: {"frontLightSettings": {"brightness": 0}}},
            light_set_at={CAM_A: time.monotonic() - 999},
            WRITE_LOCK_SECS=30.0,
            shc_state_cache={CAM_A: {"camera_light": False}},
        )
        session = _make_session(
            {
                "/lighting/switch": _make_resp(
                    200, {"frontLightSettings": {"brightness": 80}}
                )
            }
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(is_gen2=True, is_online=True), session, HEADERS
        )
        assert coord.lighting_switch_cache[CAM_A] == {
            "frontLightSettings": {"brightness": 80}
        }, "poll must resume once the write-lock TTL has elapsed"

    @pytest.mark.asyncio
    async def test_lighting_switch_not_blocked_by_on_write(self):
        """GitHub #66 round-2 finding: an ON write also stamps light_set_at
        but never touches lighting_switch_cache — gating the poll on the
        timestamp alone (ignoring which direction it protects) would block
        the ONLY corrective fetch for a camera that was just turned back ON,
        prolonging the stale post-OFF zeroed brightness instead of fixing it.
        """
        coord = _make_control_coord(
            lighting_switch_cache={CAM_A: {"frontLightSettings": {"brightness": 0}}},
            light_set_at={CAM_A: time.monotonic()},
            shc_state_cache={CAM_A: {"camera_light": True}},
        )
        session = _make_session(
            {
                "/lighting/switch": _make_resp(
                    200, {"frontLightSettings": {"brightness": 80}}
                )
            }
        )
        await _poll_cam_control(
            coord, CAM_A, _ctx(is_gen2=True, is_online=True), session, HEADERS
        )
        assert coord.lighting_switch_cache[CAM_A] == {
            "frontLightSettings": {"brightness": 80}
        }, "poll must NOT be blocked by a fresh ON write, which never zeroed the cache"


def _make_slow_coord(**overrides):
    write_lock_secs = overrides.pop("WRITE_LOCK_SECS", 10.0)
    coord = SimpleNamespace(
        WRITE_LOCK_SECS=write_lock_secs,
        wifiinfo_cache={},
        ambient_light_cache={},
        motion_set_at={},
        firmware_set_at={},
        firmware_cache={},
        privacy_sound_set_at={},
        privacy_sound_cache={},
        commissioned_cache={},
        timestamp_set_at={},
        timestamp_cache={},
        notifications_cache={},
        rules_cache={},
        cloud_zones_cache={},
        cloud_privacy_masks_cache={},
        lighting_options_cache={},
        lighting_options_set_at={},
        ledlights_set_at={},
        ledlights_cache={},
        lens_elevation_cache={},
        audio_cache={},
        motion_light_cache={},
        ambient_lighting_cache={},
        global_lighting_cache={},
        intrusion_config_set_at={},
        intrusion_config_cache={},
        audio_detection_set_at={},
        audio_detection_cache={},
        alarm_settings_set_at={},
        alarm_settings_cache={},
        alarm_status_cache={},
        arming_set_at={},
        arming_cache={},
        icon_led_brightness_cache={},
        gen2_zones_cache={},
        gen2_private_areas_cache={},
    )

    def is_write_locked(cam_id, set_at_dict):
        ts = set_at_dict.get(cam_id)
        return ts is not None and (time.monotonic() - ts) < coord.WRITE_LOCK_SECS

    coord.is_write_locked = is_write_locked
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _slow_ctx(**overrides):
    defaults = dict(
        hw="CAMERA",
        is_gen2=False,
        is_online=True,
        stream_active=False,
        local_stream_active=False,
        privacy_on=False,
        do_slow_cam=True,
        pan_limit=0,
        has_light=False,
    )
    defaults.update(overrides)
    return CamContext(**defaults)


def _all_endpoints_session(json_map: dict):
    """Session mock keyed by URL suffix — matches whichever key is a
    substring of the requested URL. Each value is a JSON body (200)."""

    def _get(url, **kwargs):
        url_str = str(url)
        for suffix, body in json_map.items():
            if url_str.endswith(f"/{suffix}"):
                return _make_resp(200, body)
        return _make_resp(404)

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


NOOP_INTRUSION = MagicMock()


class TestPollSlowTierGating:
    @pytest.mark.asyncio
    async def test_skipped_when_do_slow_cam_false(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"wifiinfo": {"ssid": "x"}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(do_slow_cam=False),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_offline(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"wifiinfo": {"ssid": "x"}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(is_online=False),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        session.get.assert_not_called()


class TestPollSlowTierGen1AllEndpoints:
    @pytest.mark.asyncio
    async def test_gen1_endpoints_populate_all_caches(self):
        coord = _make_slow_coord()
        data = {CAM_A: {}}
        session = _all_endpoints_session(
            {
                "wifiinfo": {"ssid": "home"},
                "ambient_light_sensor_level": {"ambientLightSensorLevel": 50},
                "motion": {"sensitivity": 5},
                "firmware": {"upToDate": True},
                "recording_options": {"enabled": True},
                "commissioned": {"connected": True},
                "timestamp": {"result": True},
                "notifications": {"movement": True},
                "rules": [{"id": "r1"}],
                "motion_sensitive_areas": [{"id": "z1"}],
                "privacy_masks": [{"id": "m1"}],
                "privacy_sound_override": {"result": True},
            }
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {"title": "Cam"},
            _slow_ctx(hw="CAMERA", is_gen2=False),
            data,
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.wifiinfo_cache[CAM_A] == {"ssid": "home"}
        assert coord.ambient_light_cache[CAM_A] == 50
        assert data[CAM_A]["motion"] == {"sensitivity": 5}
        assert coord.firmware_cache[CAM_A] == {"upToDate": True}
        assert data[CAM_A]["recordingOptions"] == {"enabled": True}
        assert coord.commissioned_cache[CAM_A] == {"connected": True}
        assert coord.timestamp_cache[CAM_A] is True
        assert coord.notifications_cache[CAM_A] == {"movement": True}
        assert coord.rules_cache[CAM_A] == [{"id": "r1"}]
        assert coord.cloud_zones_cache[CAM_A] == [{"id": "z1"}]
        assert coord.cloud_privacy_masks_cache[CAM_A] == [{"id": "m1"}]
        # privacy_sound_override is only polled for INDOOR/CAMERA_360/
        # HOME_Eyes_Indoor/CAMERA_INDOOR_GEN2 — not this hw ("CAMERA" =
        # Eyes Outdoor Gen1) — covered separately below.
        assert CAM_A not in coord.privacy_sound_cache

    @pytest.mark.asyncio
    async def test_indoor_gen1_privacy_sound_override_included(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"privacy_sound_override": {"result": False}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="INDOOR"),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.privacy_sound_cache[CAM_A] is False

    @pytest.mark.asyncio
    async def test_autofollow_and_lighting_options_gated_on_ctx(self):
        coord = _make_slow_coord()
        data = {CAM_A: {}}
        session = _all_endpoints_session(
            {"autofollow": {"enabled": True}, "lighting_options": {"opt": 1}}
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(pan_limit=180, has_light=True),
            data,
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert data[CAM_A]["autofollow"] == {"enabled": True}
        assert coord.lighting_options_cache[CAM_A] == {"opt": 1}

    @pytest.mark.asyncio
    async def test_autofollow_lighting_options_absent_when_not_gated(self):
        coord = _make_slow_coord()
        data = {CAM_A: {}}
        session = _all_endpoints_session(
            {"autofollow": {"enabled": True}, "lighting_options": {"opt": 1}}
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(pan_limit=0, has_light=False),
            data,
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert "autofollow" not in data[CAM_A]
        assert CAM_A not in coord.lighting_options_cache


class TestPollSlowTierGen2Endpoints:
    @pytest.mark.asyncio
    async def test_gen2_zones_and_private_areas(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session(
            {"zones": [{"id": "z1"}], "privateAreas": [{"id": "p1"}]}
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Outdoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.gen2_zones_cache[CAM_A] == [{"id": "z1"}]
        assert coord.gen2_private_areas_cache[CAM_A] == [{"id": "p1"}]

    @pytest.mark.asyncio
    async def test_gen2_indoor_ii_skips_private_areas(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"privateAreas": [{"id": "p1"}]})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert CAM_A not in coord.gen2_private_areas_cache

    @pytest.mark.asyncio
    async def test_gen2_only_endpoints_populate_caches(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session(
            {
                "ledlights": {"state": "ON"},
                "lens_elevation": {"elevation": 12},
                "audio": {"volume": 5},
                "lighting/motion": {"enabled": True},
                "lighting/ambient": {"enabled": False},
                "lighting": {"mode": "auto"},
                "intrusionDetectionConfig": {"enabled": True},
                "audioDetectionConfig": {"detectGlassBreak": True},
            }
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Outdoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.ledlights_cache[CAM_A] is True
        assert coord.lens_elevation_cache[CAM_A] == 12
        assert coord.audio_cache[CAM_A] == {"volume": 5}
        assert coord.motion_light_cache[CAM_A] == {"enabled": True}
        assert coord.ambient_lighting_cache[CAM_A] == {"enabled": False}
        assert coord.global_lighting_cache[CAM_A] == {"mode": "auto"}
        assert coord.intrusion_config_cache[CAM_A] == {"enabled": True}
        assert coord.audio_detection_cache[CAM_A] == {"detectGlassBreak": True}

    @pytest.mark.asyncio
    async def test_ledlights_off_state(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"ledlights": {"state": "OFF"}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Outdoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.ledlights_cache[CAM_A] is False

    @pytest.mark.asyncio
    async def test_ledlights_non_dict_defaults_none(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"ledlights": "not-a-dict"})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Outdoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.ledlights_cache[CAM_A] is None


class TestPollSlowTierMalformedDictCacheEndpoints:
    """Regression: wifiinfo/firmware/commissioned/notifications previously had
    no isinstance(dict) guard against a malformed-but-200 cloud response,
    unlike sibling branches in the same dispatcher (ambient_light_sensor_level/
    privacy_sound_override/timestamp) already hardened in the v15.0.0
    chaos-fault-injection round. A JSON array/string/number body instead of an
    object would have crashed the whole coordinator tick uncaught on the next
    consumer's unguarded dict access. Found during HA-Core-submission-prep
    mypy work 2026-07-15 while widening _fetch()'s return type annotation.
    """

    @pytest.mark.asyncio
    async def test_wifiinfo_non_dict_skips_cache_write(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"wifiinfo": "not-a-dict"})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="CAMERA", is_gen2=False),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert CAM_A not in coord.wifiinfo_cache

    @pytest.mark.asyncio
    async def test_firmware_non_dict_skips_cache_write(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"firmware": "not-a-dict"})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="CAMERA", is_gen2=False),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert CAM_A not in coord.firmware_cache

    @pytest.mark.asyncio
    async def test_commissioned_non_dict_skips_cache_write(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"commissioned": "not-a-dict"})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="CAMERA", is_gen2=False),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert CAM_A not in coord.commissioned_cache

    @pytest.mark.asyncio
    async def test_notifications_non_dict_skips_cache_write(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"notifications": "not-a-dict"})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="CAMERA", is_gen2=False),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert CAM_A not in coord.notifications_cache


class TestPollSlowTierIndoorIIAlarm:
    @pytest.mark.asyncio
    async def test_alarm_settings_status_led_populate(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session(
            {
                "alarm_settings": {"armed": True},
                "alarmStatus": {"alarmType": "NONE", "intrusionSystem": "ACTIVE"},
                "iconLedBrightness": {"value": 3},
            }
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {"title": "Haustuere"},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.alarm_settings_cache[CAM_A] == {"armed": True}
        assert coord.alarm_status_cache[CAM_A]["intrusionSystem"] == "ACTIVE"
        assert coord.arming_cache[CAM_A] is True
        assert coord.icon_led_brightness_cache[CAM_A] == 3

    @pytest.mark.asyncio
    async def test_arming_inactive(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session(
            {"alarmStatus": {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}}
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.arming_cache[CAM_A] is False

    @pytest.mark.asyncio
    async def test_icon_led_brightness_clamped(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"iconLedBrightness": {"value": 99}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.icon_led_brightness_cache[CAM_A] == 4

    @pytest.mark.asyncio
    async def test_icon_led_brightness_invalid_value_defaults_zero(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"iconLedBrightness": {"value": "nope"}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.icon_led_brightness_cache[CAM_A] == 0

    @pytest.mark.asyncio
    async def test_intrusion_event_callback_invoked_on_dict_alarm_status(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session(
            {"alarmStatus": {"alarmType": "INTRUSION_DETECTED"}}
        )
        fire = MagicMock()
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {"title": "Haustuere"},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            fire,
        )
        fire.assert_called_once_with(
            CAM_A, "Haustuere", {"alarmType": "INTRUSION_DETECTED"}
        )


class TestPollSlowTierWriteLockSkips:
    @pytest.mark.asyncio
    async def test_motion_write_locked_not_overwritten(self):
        coord = _make_slow_coord(motion_set_at={CAM_A: time.monotonic()})
        data = {CAM_A: {"motion": "stale"}}
        session = _all_endpoints_session({"motion": {"sensitivity": 9}})
        await _poll_slow_tier_endpoints(
            coord, CAM_A, {}, _slow_ctx(), data, session, HEADERS, NOOP_INTRUSION
        )
        assert data[CAM_A]["motion"] == "stale"

    @pytest.mark.asyncio
    async def test_firmware_write_locked_not_overwritten(self):
        coord = _make_slow_coord(firmware_set_at={CAM_A: time.monotonic()})
        coord.firmware_cache[CAM_A] = "stale"
        session = _all_endpoints_session({"firmware": {"upToDate": False}})
        await _poll_slow_tier_endpoints(
            coord, CAM_A, {}, _slow_ctx(), {CAM_A: {}}, session, HEADERS, NOOP_INTRUSION
        )
        assert coord.firmware_cache[CAM_A] == "stale"

    @pytest.mark.asyncio
    async def test_privacy_sound_write_locked_not_overwritten(self):
        coord = _make_slow_coord(privacy_sound_set_at={CAM_A: time.monotonic()})
        coord.privacy_sound_cache[CAM_A] = True
        session = _all_endpoints_session({"privacy_sound_override": {"result": False}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="INDOOR"),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.privacy_sound_cache[CAM_A] is True

    @pytest.mark.asyncio
    async def test_timestamp_write_locked_not_overwritten(self):
        coord = _make_slow_coord(timestamp_set_at={CAM_A: time.monotonic()})
        coord.timestamp_cache[CAM_A] = True
        session = _all_endpoints_session({"timestamp": {"result": False}})
        await _poll_slow_tier_endpoints(
            coord, CAM_A, {}, _slow_ctx(), {CAM_A: {}}, session, HEADERS, NOOP_INTRUSION
        )
        assert coord.timestamp_cache[CAM_A] is True

    @pytest.mark.asyncio
    async def test_ledlights_write_locked_not_overwritten(self):
        coord = _make_slow_coord(ledlights_set_at={CAM_A: time.monotonic()})
        coord.ledlights_cache[CAM_A] = True
        session = _all_endpoints_session({"ledlights": {"state": "OFF"}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Outdoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.ledlights_cache[CAM_A] is True

    @pytest.mark.asyncio
    async def test_intrusion_config_write_locked_not_overwritten(self):
        coord = _make_slow_coord(intrusion_config_set_at={CAM_A: time.monotonic()})
        coord.intrusion_config_cache[CAM_A] = {"enabled": True}
        session = _all_endpoints_session(
            {"intrusionDetectionConfig": {"enabled": False}}
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Outdoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.intrusion_config_cache[CAM_A] == {"enabled": True}

    @pytest.mark.asyncio
    async def test_audio_detection_write_locked_not_overwritten(self):
        coord = _make_slow_coord(audio_detection_set_at={CAM_A: time.monotonic()})
        coord.audio_detection_cache[CAM_A] = {"detectGlassBreak": True}
        session = _all_endpoints_session(
            {"audioDetectionConfig": {"detectGlassBreak": False}}
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Outdoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.audio_detection_cache[CAM_A] == {"detectGlassBreak": True}

    @pytest.mark.asyncio
    async def test_alarm_settings_write_locked_not_overwritten(self):
        coord = _make_slow_coord(alarm_settings_set_at={CAM_A: time.monotonic()})
        coord.alarm_settings_cache[CAM_A] = {"armed": True}
        session = _all_endpoints_session({"alarm_settings": {"armed": False}})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.alarm_settings_cache[CAM_A] == {"armed": True}

    @pytest.mark.asyncio
    async def test_arming_write_locked_not_overwritten_but_status_cache_still_updates(
        self,
    ):
        coord = _make_slow_coord(arming_set_at={CAM_A: time.monotonic()})
        coord.arming_cache[CAM_A] = True
        session = _all_endpoints_session(
            {"alarmStatus": {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}}
        )
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        # arming write-locked → stays True; alarm_status_cache unconditionally updates
        assert coord.arming_cache[CAM_A] is True
        assert coord.alarm_status_cache[CAM_A]["intrusionSystem"] == "INACTIVE"


class TestPollSlowTierNonDictFallbacks:
    @pytest.mark.asyncio
    async def test_rules_non_list_defaults_empty(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"rules": "not-a-list"})
        await _poll_slow_tier_endpoints(
            coord, CAM_A, {}, _slow_ctx(), {CAM_A: {}}, session, HEADERS, NOOP_INTRUSION
        )
        assert coord.rules_cache[CAM_A] == []

    @pytest.mark.asyncio
    async def test_audio_non_dict_defaults_empty(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({"audio": "not-a-dict"})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Outdoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            NOOP_INTRUSION,
        )
        assert coord.audio_cache[CAM_A] == {}

    @pytest.mark.asyncio
    async def test_alarm_status_non_dict_skips_arming_and_intrusion_event(self):
        coord = _make_slow_coord()
        fire = MagicMock()
        session = _all_endpoints_session({"alarmStatus": "not-a-dict"})
        await _poll_slow_tier_endpoints(
            coord,
            CAM_A,
            {},
            _slow_ctx(hw="HOME_Eyes_Indoor", is_gen2=True),
            {CAM_A: {}},
            session,
            HEADERS,
            fire,
        )
        assert coord.alarm_status_cache[CAM_A] == {}
        assert CAM_A not in coord.arming_cache
        fire.assert_not_called()


class TestPollSlowTierNon200AndExceptions:
    @pytest.mark.asyncio
    async def test_non_200_status_not_cached(self):
        coord = _make_slow_coord()
        session = _all_endpoints_session({})  # wifiinfo falls through to 404
        await _poll_slow_tier_endpoints(
            coord, CAM_A, {}, _slow_ctx(), {CAM_A: {}}, session, HEADERS, NOOP_INTRUSION
        )
        assert CAM_A not in coord.wifiinfo_cache

    @pytest.mark.asyncio
    async def test_fetch_exception_swallowed_no_cache_write(self):
        coord = _make_slow_coord()

        def _raise(*_a, **_k):
            raise RuntimeError("boom")

        session = MagicMock()
        session.get = MagicMock(side_effect=_raise)
        await _poll_slow_tier_endpoints(
            coord, CAM_A, {}, _slow_ctx(), {CAM_A: {}}, session, HEADERS, NOOP_INTRUSION
        )
        assert CAM_A not in coord.wifiinfo_cache

    @pytest.mark.asyncio
    async def test_cancelled_error_in_one_endpoint_does_not_abort_others(self):
        """A CancelledError escaping one _fetch (not caught by its own
        `except Exception`) must not stop the other endpoints' results
        from being processed — asyncio.gather(return_exceptions=True)
        isolates it into the BaseException-skip branch."""
        coord = _make_slow_coord()

        def _get(url, **kwargs):
            url_str = str(url)
            if url_str.endswith("/wifiinfo"):
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError())
                cm.__aexit__ = AsyncMock(return_value=None)
                return cm
            if url_str.endswith("/ambient_light_sensor_level"):
                return _make_resp(200, {"ambientLightSensorLevel": 10})
            return _make_resp(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)
        await _poll_slow_tier_endpoints(
            coord, CAM_A, {}, _slow_ctx(), {CAM_A: {}}, session, HEADERS, NOOP_INTRUSION
        )
        assert CAM_A not in coord.wifiinfo_cache
        assert coord.ambient_light_cache[CAM_A] == 10
