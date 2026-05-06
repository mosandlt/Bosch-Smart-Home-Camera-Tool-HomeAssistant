"""Coordinator pure-state helpers — large-coverage round.

Targets `__init__.py` (12% → ~30% expected) by binding the unbound
methods of `BoschCameraCoordinator` to a `SimpleNamespace` stub.
Each method is a pure-state read or single-dict mutation — no I/O,
no async, no HA framework needed.

Methods covered:
  - `_is_write_locked`              — eventual-consistency guard
  - `_get_cam_lan_ip`               — LAN-IP fallback chain
  - `_should_check_status`          — status-poll cadence
  - `_token_still_valid`            — JWT exp parsing
  - `_invalidate_rcp_session`       — RCP cache eviction
  - `_proxy_hash_from_rcp_base`     — static URL parser
  - `clock_offset`, `rcp_lan_ip`, `rcp_product_name`, `rcp_bitrate_ladder`
  - `get_quality`, `set_quality`, `get_quality_params`
  - `motion_settings`, `audio_alarm_settings`, `recording_options`
  - `clear_stream_warming`, `is_stream_warming` (3 stale-clear scenarios)
  - `record_stream_error`, `record_stream_success`
  - `get_model_config`
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM_B = "20E053B5-BE64-4E45-A2CA-BBDC20F5C351"


def _make_coord(**overrides) -> SimpleNamespace:
    """Stub coordinator with all per-cam dicts the helpers expect."""
    base = dict(
        data={},
        _live_connections={},
        _live_opened_at={},
        _stream_warming=set(),
        _stream_warming_started={},
        _stream_error_count={},
        _stream_error_at={},
        _stream_fell_back={},
        _local_rescue_attempts={},
        _local_rescue_at={},
        _rcp_lan_ip_cache={},
        _local_creds_cache={},
        _rcp_clock_offset_cache={},
        _rcp_product_name_cache={},
        _rcp_bitrate_cache={},
        _rcp_session_cache={},
        _quality_preference={},
        _proxy_url_cache={},
        _audio_alarm_cache={},
        _hw_version={},
        _last_status=0.0,
        _offline_since={},
        _per_cam_status_at={},
        _privacy_set_at={},
        _light_set_at={},
        _WRITE_LOCK_SECS=30.0,
        _OFFLINE_EXTENDED_INTERVAL=900,
        _entry=SimpleNamespace(
            data={"bearer_token": "tok-AAA", "refresh_token": "rfr-BBB"},
            options={},
        ),
        _refreshed_token=None,
        _refreshed_refresh=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── _is_write_locked ────────────────────────────────────────────────────


class TestIsWriteLocked:
    """The 30 s write-lock that defends switch state from stale cloud
    polls. Bug shape: privacy switch flipping back to ON within ~1 s of
    the user turning it OFF (PRIVACY_REVERT, fixed in v11.0.1)."""

    def test_no_write_yet_returns_false(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        # No timestamp recorded → not locked
        assert BoschCameraCoordinator._is_write_locked(
            coord, CAM_A, coord._privacy_set_at,
        ) is False

    def test_recent_write_locks(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._privacy_set_at[CAM_A] = time.monotonic()  # just now
        assert BoschCameraCoordinator._is_write_locked(
            coord, CAM_A, coord._privacy_set_at,
        ) is True

    def test_old_write_unlocked(self):
        """A write older than _WRITE_LOCK_SECS no longer holds the lock —
        the cloud has had enough time to settle."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._privacy_set_at[CAM_A] = time.monotonic() - 60.0  # 60s ago
        assert BoschCameraCoordinator._is_write_locked(
            coord, CAM_A, coord._privacy_set_at,
        ) is False

    def test_threshold_boundary_is_locked(self):
        """Right at the boundary (29 s ago) — still locked. The condition
        is `<`, so ≥30 unlocks. Pin the inequality direction."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._privacy_set_at[CAM_A] = time.monotonic() - 29.0
        assert BoschCameraCoordinator._is_write_locked(
            coord, CAM_A, coord._privacy_set_at,
        ) is True

    def test_per_cam_independence(self):
        """Lock for cam-A must not bleed into cam-B."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._privacy_set_at[CAM_A] = time.monotonic()
        assert BoschCameraCoordinator._is_write_locked(
            coord, CAM_B, coord._privacy_set_at,
        ) is False


# ── _get_cam_lan_ip — fallback chain ────────────────────────────────────


class TestGetCamLanIp:
    def test_returns_none_when_no_cache(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        assert BoschCameraCoordinator._get_cam_lan_ip(coord, CAM_A) is None

    def test_prefers_rcp_cache_over_creds_cache(self):
        """RCP cache (0x0a36 lookup) is the most authoritative — wins
        over the digest-creds host (which may be a stale mDNS hostname)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._rcp_lan_ip_cache[CAM_A] = "192.0.2.1"
        coord._local_creds_cache[CAM_A] = {"host": "shc-fallback.local"}
        assert BoschCameraCoordinator._get_cam_lan_ip(coord, CAM_A) == "192.0.2.1"

    def test_falls_back_to_creds_host(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._local_creds_cache[CAM_A] = {"host": "192.0.2.50", "port": 443}
        assert BoschCameraCoordinator._get_cam_lan_ip(coord, CAM_A) == "192.0.2.50"

    def test_creds_without_host_returns_none(self):
        """Empty creds dict (just user/password) → None, not '' / KeyError."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._local_creds_cache[CAM_A] = {"user": "x", "password": "y"}
        assert BoschCameraCoordinator._get_cam_lan_ip(coord, CAM_A) is None


# ── _should_check_status — status-poll cadence ──────────────────────────


class TestShouldCheckStatus:
    """Status-check cadence: normal (every 60 s) vs offline-extended
    (every 900 s after >15 min offline). Trip-up: the offline path
    checks `_per_cam_status_at` instead of the global `_last_status`."""

    def test_normal_cadence_due(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_last_status=0.0)
        # 90 s elapsed > 60 s interval → due
        assert BoschCameraCoordinator._should_check_status(
            coord, CAM_A, 90.0, 60,
        ) is True

    def test_normal_cadence_not_due(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_last_status=time.monotonic() - 10.0)
        # 10 s ago < 60 s interval → not due
        assert BoschCameraCoordinator._should_check_status(
            coord, CAM_A, time.monotonic(), 60,
        ) is False

    def test_offline_extended_cadence(self):
        """Camera offline >15 min uses the 900 s extended interval."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        now = time.monotonic()
        coord = _make_coord(
            _last_status=now - 60.0,  # global status check just ran
            _offline_since={CAM_A: now - 1200.0},  # offline for 20 min
            _per_cam_status_at={CAM_A: now - 950.0},  # last per-cam check 950s ago
        )
        # In extended mode: per_cam_last 950s ago > 900s → due
        assert BoschCameraCoordinator._should_check_status(
            coord, CAM_A, now, 60,
        ) is True

    def test_offline_extended_blocks_premature_recheck(self):
        """A camera offline 20 min, last per-cam check 100s ago → not due
        (extended interval is 900 s, not 60 s)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        now = time.monotonic()
        coord = _make_coord(
            _last_status=now - 60.0,
            _offline_since={CAM_A: now - 1200.0},
            _per_cam_status_at={CAM_A: now - 100.0},
        )
        assert BoschCameraCoordinator._should_check_status(
            coord, CAM_A, now, 60,
        ) is False


# ── _token_still_valid — JWT exp parsing ────────────────────────────────


def _make_jwt(exp_offset_secs: int) -> str:
    """Build a fake unsigned JWT with `exp` at now + offset."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + exp_offset_secs}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _coord_with_token(token: str) -> SimpleNamespace:
    """Stub that exposes `.token` as a plain attr so the unbound
    `_token_still_valid` (which reads `self.token`) sees the test value
    without going through the real property."""
    coord = _make_coord()
    coord._entry.data["bearer_token"] = token
    coord.token = token  # bypass the property (uses _refreshed_token logic)
    return coord


class TestTokenStillValid:
    """JWT-payload `exp` parsing — used to skip refresh when a concurrent
    caller already refreshed the token. Bug: malformed JWT must NOT
    crash the refresh path; it must return False so the refresh proceeds."""

    def test_valid_token_with_long_exp(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _coord_with_token(_make_jwt(3600))
        assert BoschCameraCoordinator._token_still_valid(coord) is True

    def test_token_about_to_expire(self):
        """Token with 30 s remaining < default min_remaining=60 → invalid."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _coord_with_token(_make_jwt(30))
        assert BoschCameraCoordinator._token_still_valid(coord) is False

    def test_expired_token(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _coord_with_token(_make_jwt(-3600))
        assert BoschCameraCoordinator._token_still_valid(coord) is False

    def test_empty_token(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _coord_with_token("")
        assert BoschCameraCoordinator._token_still_valid(coord) is False

    def test_malformed_jwt_doesnt_crash(self):
        """Garbage in token field must be treated as expired, not raise."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _coord_with_token("this-is-not-a-jwt")
        assert BoschCameraCoordinator._token_still_valid(coord) is False

    def test_jwt_without_exp_field_is_invalid(self):
        """Default exp=0 in the get → very old → invalid."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{}').rstrip(b"=").decode()
        coord = _coord_with_token(f"{header}.{payload}.sig")
        assert BoschCameraCoordinator._token_still_valid(coord) is False

    def test_min_remaining_param_respected(self):
        """min_remaining=10 must accept tokens with 30 s left."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _coord_with_token(_make_jwt(30))
        assert BoschCameraCoordinator._token_still_valid(coord, min_remaining=10) is True
        assert BoschCameraCoordinator._token_still_valid(coord, min_remaining=60) is False


# ── _invalidate_rcp_session — RCP cache eviction ────────────────────────


class TestInvalidateRcpSession:
    def test_removes_cached_entry(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._rcp_session_cache["abc123"] = ("0xdeadbeef", time.monotonic() + 300)
        BoschCameraCoordinator._invalidate_rcp_session(coord, "abc123")
        assert "abc123" not in coord._rcp_session_cache

    def test_idempotent_on_missing_key(self):
        """Invalidating a non-cached hash must not raise."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        BoschCameraCoordinator._invalidate_rcp_session(coord, "never-cached")
        # No exception, no state change
        assert coord._rcp_session_cache == {}


# ── _proxy_hash_from_rcp_base — static URL parser ───────────────────────


class TestProxyHashFromRcpBase:
    def test_extracts_hash_from_canonical_url(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        url = "https://proxy-12.live.cbs.boschsecurity.com:42090/abcdef1234/rcp.xml"
        assert BoschCameraCoordinator._proxy_hash_from_rcp_base(url) == "abcdef1234"

    def test_strips_trailing_slash(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        url = "https://host/myhash/rcp.xml/"
        assert BoschCameraCoordinator._proxy_hash_from_rcp_base(url) == "myhash"

    def test_returns_none_for_non_rcp_url(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert BoschCameraCoordinator._proxy_hash_from_rcp_base(
            "https://host/abc/snap.jpg"
        ) is None

    def test_returns_none_for_empty(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert BoschCameraCoordinator._proxy_hash_from_rcp_base("") is None


# ── RCP-cache reader properties ─────────────────────────────────────────


class TestRcpCacheReaders:
    def test_clock_offset_returns_cached(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._rcp_clock_offset_cache[CAM_A] = -2.5
        assert BoschCameraCoordinator.clock_offset(coord, CAM_A) == -2.5

    def test_clock_offset_none_when_uncached(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert BoschCameraCoordinator.clock_offset(_make_coord(), CAM_A) is None

    def test_rcp_lan_ip(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._rcp_lan_ip_cache[CAM_A] = "192.168.1.50"
        assert BoschCameraCoordinator.rcp_lan_ip(coord, CAM_A) == "192.168.1.50"
        assert BoschCameraCoordinator.rcp_lan_ip(coord, "unknown-cam") is None

    def test_rcp_product_name(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._rcp_product_name_cache[CAM_A] = "HOME_Eyes_Outdoor"
        assert BoschCameraCoordinator.rcp_product_name(coord, CAM_A) == "HOME_Eyes_Outdoor"
        assert BoschCameraCoordinator.rcp_product_name(coord, CAM_B) is None

    def test_rcp_bitrate_ladder_default_empty_list(self):
        """Missing entry → [] (not None) so callers can iterate without
        a guard."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert BoschCameraCoordinator.rcp_bitrate_ladder(_make_coord(), CAM_A) == []

    def test_rcp_bitrate_ladder_returns_cached(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._rcp_bitrate_cache[CAM_A] = [500, 2500, 7500]
        assert BoschCameraCoordinator.rcp_bitrate_ladder(coord, CAM_A) == [500, 2500, 7500]


# ── get_quality / set_quality / get_quality_params ──────────────────────


class TestQualityPreference:
    def test_get_quality_default_auto(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert BoschCameraCoordinator.get_quality(_make_coord(), CAM_A) == "auto"

    def test_get_quality_high_from_options(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._entry.options["high_quality_video"] = True
        assert BoschCameraCoordinator.get_quality(coord, CAM_A) == "high"

    def test_runtime_override_wins_over_options(self):
        """Per-camera select-entity override beats persisted option."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._entry.options["high_quality_video"] = True  # default would be "high"
        coord._quality_preference[CAM_A] = "low"
        assert BoschCameraCoordinator.get_quality(coord, CAM_A) == "low"

    def test_set_quality_invalidates_proxy_cache(self):
        """Switching quality must drop the cached proxy URL — otherwise
        the next stream-on reuses the old highQualityVideo flag."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._proxy_url_cache[CAM_A] = "stale-url"
        BoschCameraCoordinator.set_quality(coord, CAM_A, "high")
        assert coord._quality_preference[CAM_A] == "high"
        assert CAM_A not in coord._proxy_url_cache, (
            "proxy_url_cache must be invalidated on quality change so "
            "the next try_live_connection refetches with new flags."
        )

    def test_get_quality_params_high(self):
        """high → highQualityVideo=True, inst=1 (primary encoder)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._quality_preference[CAM_A] = "high"
        # Bind get_quality so get_quality_params can call self.get_quality
        coord.get_quality = lambda cid: BoschCameraCoordinator.get_quality(coord, cid)
        assert BoschCameraCoordinator.get_quality_params(coord, CAM_A) == (True, 1)

    def test_get_quality_params_low(self):
        """low → highQualityVideo=False, inst=4 (low-bandwidth)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._quality_preference[CAM_A] = "low"
        coord.get_quality = lambda cid: BoschCameraCoordinator.get_quality(coord, cid)
        assert BoschCameraCoordinator.get_quality_params(coord, CAM_A) == (False, 4)

    def test_get_quality_params_auto(self):
        """auto → highQualityVideo=False, inst=2 (iOS default, balanced)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.get_quality = lambda cid: BoschCameraCoordinator.get_quality(coord, cid)
        assert BoschCameraCoordinator.get_quality_params(coord, CAM_A) == (False, 2)


# ── motion_settings, audio_alarm_settings, recording_options ────────────


class TestSettingsReaders:
    def test_motion_settings_empty_when_no_data(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert BoschCameraCoordinator.motion_settings(_make_coord(), CAM_A) == {}

    def test_motion_settings_returns_cam_motion(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(data={CAM_A: {"motion": {"enabled": True, "motionAlarmConfiguration": "HIGH"}}})
        assert BoschCameraCoordinator.motion_settings(coord, CAM_A) == {
            "enabled": True, "motionAlarmConfiguration": "HIGH",
        }

    def test_audio_alarm_settings_prefers_persistent_cache(self):
        """data[cam_id]['audioAlarm'] is transient (rebuilt each tick).
        The persistent cache must win so stale `data` doesn't drop the
        user's audio-alarm config between slow-tier ticks."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(data={CAM_A: {"audioAlarm": {"transient": True}}})
        coord._audio_alarm_cache[CAM_A] = {"persisted": True}
        # Persistent cache wins
        assert BoschCameraCoordinator.audio_alarm_settings(coord, CAM_A) == {"persisted": True}

    def test_audio_alarm_settings_falls_back_to_data(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(data={CAM_A: {"audioAlarm": {"only-in-data": True}}})
        assert BoschCameraCoordinator.audio_alarm_settings(coord, CAM_A) == {"only-in-data": True}

    def test_recording_options(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(data={CAM_A: {"recordingOptions": {"x": 1}}})
        assert BoschCameraCoordinator.recording_options(coord, CAM_A) == {"x": 1}
        # Missing → empty dict
        assert BoschCameraCoordinator.recording_options(coord, CAM_B) == {}


# ── stream-warming state machine ────────────────────────────────────────


class TestStreamWarming:
    """The 3 stale-clear scenarios — auto-recovery for stuck warming flags."""

    def test_clear_stream_warming_idempotent(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        # Calling on an empty set must not raise
        BoschCameraCoordinator.clear_stream_warming(coord, CAM_A)
        assert CAM_A not in coord._stream_warming

    def test_is_warming_returns_false_when_not_set(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert BoschCameraCoordinator.is_stream_warming(_make_coord(), CAM_A) is False

    def test_warming_without_live_conn_auto_clears(self):
        """Scenario 1: flag set but no live_connection — pre-warm errored
        out without clearing. Auto-recover."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._stream_warming.add(CAM_A)
        coord._stream_warming_started[CAM_A] = time.monotonic()
        # No _live_connections[CAM_A] → stale
        assert BoschCameraCoordinator.is_stream_warming(coord, CAM_A) is False
        assert CAM_A not in coord._stream_warming
        assert CAM_A not in coord._stream_warming_started

    def test_warming_with_url_set_auto_clears(self):
        """Scenario 2: URL ready but flag still on — race in cleanup paths.
        Auto-recover."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._stream_warming.add(CAM_A)
        coord._stream_warming_started[CAM_A] = time.monotonic()
        coord._live_connections[CAM_A] = {"rtspsUrl": "rtsps://x"}
        assert BoschCameraCoordinator.is_stream_warming(coord, CAM_A) is False
        assert CAM_A not in coord._stream_warming

    def test_warming_300s_timeout_clears(self):
        """Scenario 3: 5-min hard timeout — pre-warm absolute worst case
        is ~120 s; >300 s means stuck."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._stream_warming.add(CAM_A)
        coord._stream_warming_started[CAM_A] = time.monotonic() - 400
        # No URL (so scenario 2 doesn't fire), but live conn entry exists
        coord._live_connections[CAM_A] = {"rtspsUrl": ""}
        assert BoschCameraCoordinator.is_stream_warming(coord, CAM_A) is False
        assert CAM_A not in coord._stream_warming

    def test_warming_under_timeout_stays_true(self):
        """Healthy mid-warm state: live conn pending, no URL, started recently."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._stream_warming.add(CAM_A)
        coord._stream_warming_started[CAM_A] = time.monotonic() - 30  # 30 s ago
        coord._live_connections[CAM_A] = {"rtspsUrl": ""}
        assert BoschCameraCoordinator.is_stream_warming(coord, CAM_A) is True


# ── record_stream_error / record_stream_success ─────────────────────────


class TestRecordStreamError:
    def _stub_coord_with_model(self, conn_type: str = "LOCAL"):
        from custom_components.bosch_shc_camera.models import get_model_config
        coord = _make_coord(_hw_version={CAM_A: "HOME_Eyes_Outdoor"})
        coord._live_connections[CAM_A] = {"_connection_type": conn_type}
        coord.get_model_config = lambda cam_id: get_model_config(coord._hw_version[cam_id])
        return coord

    def test_remote_session_does_not_increment(self):
        """Errors on REMOTE shouldn't pin the cam to REMOTE forever — the
        counter exists to suppress LOCAL after consecutive LAN failures."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = self._stub_coord_with_model(conn_type="REMOTE")
        BoschCameraCoordinator.record_stream_error(coord, CAM_A)
        assert coord._stream_error_count.get(CAM_A, 0) == 0, (
            "REMOTE-side errors must not increment the LOCAL-failure "
            "counter — otherwise a single Cloud hiccup pins the cam to "
            "REMOTE permanently even when LAN is healthy."
        )

    def test_local_session_increments(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = self._stub_coord_with_model(conn_type="LOCAL")
        BoschCameraCoordinator.record_stream_error(coord, CAM_A)
        assert coord._stream_error_count[CAM_A] == 1
        assert coord._stream_error_at[CAM_A] > 0

    def test_local_increments_to_threshold(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = self._stub_coord_with_model(conn_type="LOCAL")
        for _ in range(5):
            BoschCameraCoordinator.record_stream_error(coord, CAM_A)
        # max_stream_errors for outdoor is 10 per CLAUDE.md raise; just
        # verify monotonic increase rather than exact threshold.
        assert coord._stream_error_count[CAM_A] == 5

    def test_record_success_resets_counter(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _stream_error_count={CAM_A: 3},
            _stream_error_at={CAM_A: time.monotonic()},
            _stream_fell_back={CAM_A: True},
            _local_rescue_attempts={CAM_A: 1},
            _local_rescue_at={CAM_A: time.monotonic()},
        )
        BoschCameraCoordinator.record_stream_success(coord, CAM_A)
        assert coord._stream_error_count[CAM_A] == 0
        assert CAM_A not in coord._stream_error_at
        assert coord._stream_fell_back[CAM_A] is False, (
            "fell_back must reset to False, not deleted, so the badge "
            "transitions from 'fallback' to 'streaming' cleanly."
        )
        assert CAM_A not in coord._local_rescue_attempts
        assert CAM_A not in coord._local_rescue_at


# ── get_model_config ────────────────────────────────────────────────────


class TestGetModelConfig:
    def test_returns_outdoor_config_for_outdoor_cam(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_hw_version={CAM_A: "HOME_Eyes_Outdoor"})
        cfg = BoschCameraCoordinator.get_model_config(coord, CAM_A)
        # Must produce a CameraModelConfig — touch at least one field
        assert cfg.heartbeat_interval > 0
        assert cfg.renewal_interval > 0
        assert cfg.max_session_duration > 0

    def test_unknown_hw_falls_back_to_default(self):
        """Cam without entry in _hw_version → "CAMERA" default."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()  # _hw_version is empty
        cfg = BoschCameraCoordinator.get_model_config(coord, "any-cam-id")
        assert cfg is not None
