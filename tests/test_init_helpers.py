"""Tests for `__init__.py` pure helpers, log filters, and resilience hooks.

Most of `__init__.py` (5146 lines) is the BoschCameraCoordinator with HA-
runtime dependencies that are hard to unit-test. But several units are
pure logic (URL guards, cred redaction, log filters) or are bound methods
that operate on a small set of coordinator dicts and can be exercised with
a `SimpleNamespace` stub.

Coverage targets in this file:

  - `_is_safe_bosch_url`               (URL allowlist, SSRF guard)
  - `_redact_creds`                    (password redaction for log lines)
  - `get_options`                      (entry options + DEFAULT_OPTIONS merge)
  - `_StreamSupportNoiseFilter`        (HA stream-component log spam filter)
  - `_install_stream_support_noise_filter`  (idempotent installer)
  - `_StreamWorkerErrorListener`       (HA stream-worker error → coordinator)
  - `is_camera_online`                 (status read)
  - `is_session_stale`                 (LOCAL keepalive give-up flag)
  - `_refresh_local_creds_from_heartbeat`  (v10.4.10 cred-rotation rescue)
  - `_FCMNoiseFilter`                  (firebase_messaging WAN-outage spam)
  - `_install_fcm_noise_filter`        (idempotent installer)

The heartbeat cred-refresh test is the load-bearing one — it pins the
v10.4.10 fix that keeps Gen2 Outdoor LOCAL streaming alive across the
~333 s Bosch session-cred rotation. Regression here means streams fail
silently after ~5 minutes.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM_B = "20E053B5-BE64-4E45-A2CA-BBDC20F5C351"


# ── _is_safe_bosch_url ─────────────────────────────────────────────────────


class TestIsSafeBoschUrl:
    """SSRF guard for image/video downloads.

    Every URL we resolve from Bosch JSON (event clip URLs, snapshot URLs)
    must point to a Bosch domain over HTTPS. Without this check a malicious
    Bosch-cloud response could redirect us into the home network or out to
    an attacker-controlled host. Pinned by source-grep so a refactor of
    the allowlist can't silently widen it."""

    def test_https_bosch_security_passes(self):
        from custom_components.bosch_shc_camera import _is_safe_bosch_url
        assert _is_safe_bosch_url(
            "https://residential.cbs.boschsecurity.com/v11/cameras/abc/snap.jpg"
        ) is True

    def test_https_bosch_com_passes(self):
        from custom_components.bosch_shc_camera import _is_safe_bosch_url
        assert _is_safe_bosch_url("https://download.bosch.com/firmware/x.bin") is True

    def test_http_rejected(self):
        """HTTPS-only — http:// must be rejected even on a Bosch host."""
        from custom_components.bosch_shc_camera import _is_safe_bosch_url
        assert _is_safe_bosch_url(
            "http://residential.cbs.boschsecurity.com/x"
        ) is False

    def test_lookalike_domain_rejected(self):
        """`boschsecurity.com.evil.example` must NOT pass — `endswith` is
        guarded by the leading `.` in the allowlist entries."""
        from custom_components.bosch_shc_camera import _is_safe_bosch_url
        assert _is_safe_bosch_url(
            "https://attacker-boschsecurity.com.evil.example/leak"
        ) is False

    def test_internal_ip_rejected(self):
        from custom_components.bosch_shc_camera import _is_safe_bosch_url
        assert _is_safe_bosch_url("https://192.168.1.1/admin") is False

    def test_localhost_rejected(self):
        from custom_components.bosch_shc_camera import _is_safe_bosch_url
        assert _is_safe_bosch_url("https://127.0.0.1:8123/api") is False

    def test_empty_url_rejected(self):
        from custom_components.bosch_shc_camera import _is_safe_bosch_url
        assert _is_safe_bosch_url("") is False

    def test_no_scheme_rejected(self):
        from custom_components.bosch_shc_camera import _is_safe_bosch_url
        # urlparse on "boschsecurity.com/path" yields scheme=""
        assert _is_safe_bosch_url("boschsecurity.com/path") is False


# ── _redact_creds ──────────────────────────────────────────────────────────


class TestRedactCreds:
    """Password redaction for log lines.

    Bosch cameras issue ephemeral Digest passwords via PUT /connection.
    They rotate often (~333 s on Gen2 Outdoor) but are still credentials
    while live. Logging them in the clear, even at DEBUG, leaks them into
    forum-attached logs (Thomas has had to scrub these manually). This
    helper redacts the password to `<3-char-prefix>***(N chars)` so the
    log line stays useful for diagnostics."""

    def test_password_redacted(self):
        from custom_components.bosch_shc_camera import _redact_creds
        out = _redact_creds({"user": "cbs-12345", "password": "supersecret"})
        assert out["user"] == "cbs-12345"
        assert out["password"] == "sup***(11 chars)"

    def test_short_password_still_redacted(self):
        """Even a 3-char password gets the redaction treatment."""
        from custom_components.bosch_shc_camera import _redact_creds
        out = _redact_creds({"password": "abc"})
        assert out["password"].startswith("abc***")
        assert "(3 chars)" in out["password"]

    def test_other_fields_passthrough(self):
        from custom_components.bosch_shc_camera import _redact_creds
        out = _redact_creds({
            "user": "cbs-99",
            "password": "x" * 20,
            "rtspsUrl": "rtsps://1.2.3.4/x",
            "_connection_type": "LOCAL",
        })
        assert out["user"] == "cbs-99"
        assert out["rtspsUrl"] == "rtsps://1.2.3.4/x"
        assert out["_connection_type"] == "LOCAL"

    def test_non_string_password_left_alone(self):
        """Defensive — if password is None/int/whatever, don't crash with
        a string slice on a non-string."""
        from custom_components.bosch_shc_camera import _redact_creds
        out = _redact_creds({"password": None, "x": 1})
        assert out["password"] is None

    def test_empty_dict(self):
        from custom_components.bosch_shc_camera import _redact_creds
        assert _redact_creds({}) == {}

    def test_returns_copy_not_mutation(self):
        """Must return a new dict — caller-side mutations of the input
        dict (live session cache) must not affect the redacted log copy."""
        from custom_components.bosch_shc_camera import _redact_creds
        original = {"password": "secret"}
        out = _redact_creds(original)
        out["password"] = "changed"
        assert original["password"] == "secret"


# ── get_options ────────────────────────────────────────────────────────────


class TestGetOptions:
    """Merge `entry.options` over `DEFAULT_OPTIONS` so missing keys take
    the default. A missed merge here means options-only changes (e.g. flip
    `enable_nvr=True`) silently fall back to the default and the user's
    selection is ignored."""

    def test_empty_options_returns_defaults(self):
        from custom_components.bosch_shc_camera import get_options
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
        entry = SimpleNamespace(options={})
        out = get_options(entry)
        for k, v in DEFAULT_OPTIONS.items():
            assert out[k] == v, f"Default {k} not preserved when options={{}}"

    def test_entry_options_override_defaults(self):
        from custom_components.bosch_shc_camera import get_options
        entry = SimpleNamespace(options={"scan_interval": 999})
        out = get_options(entry)
        assert out["scan_interval"] == 999

    def test_returns_new_dict_not_default_reference(self):
        """`DEFAULT_OPTIONS` is module-level — if we returned it directly,
        any caller-side mutation would corrupt the defaults for every
        subsequent call. Must return a copy."""
        from custom_components.bosch_shc_camera import get_options
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
        entry = SimpleNamespace(options={})
        out = get_options(entry)
        out["scan_interval"] = "MUTATED"
        # DEFAULT_OPTIONS unchanged
        assert DEFAULT_OPTIONS.get("scan_interval") != "MUTATED"


# ── _StreamSupportNoiseFilter ─────────────────────────────────────────────


class TestStreamSupportNoiseFilter:
    """Pin the rate-limited filter that suppresses HA's
    'does not support play stream service' burst during the LOCAL
    pre-warm window. Real captures show 9 of these in 15 s for a single
    stream-on; the filter collapses them to 1 line per 30 s per entity."""

    def _record(self, msg: str, level: int = logging.ERROR) -> logging.LogRecord:
        return logging.LogRecord(
            name="homeassistant.components.camera",
            level=level,
            pathname="x",
            lineno=1,
            msg=msg,
            args=None,
            exc_info=None,
        )

    def test_unrelated_messages_pass_through(self):
        from custom_components.bosch_shc_camera import _StreamSupportNoiseFilter
        f = _StreamSupportNoiseFilter()
        rec = self._record("Some other camera error")
        assert f.filter(rec) is True

    def test_first_match_passes(self):
        from custom_components.bosch_shc_camera import _StreamSupportNoiseFilter
        f = _StreamSupportNoiseFilter()
        msg = ("Error requesting stream: camera.bosch_terrasse "
               "does not support play stream service")
        rec = self._record(msg)
        assert f.filter(rec) is True

    def test_second_match_within_30s_dropped(self):
        from custom_components.bosch_shc_camera import _StreamSupportNoiseFilter
        f = _StreamSupportNoiseFilter()
        msg = ("Error requesting stream: camera.bosch_terrasse "
               "does not support play stream service")
        f.filter(self._record(msg))  # let the first one through
        # Second one within window — must be dropped
        assert f.filter(self._record(msg)) is False

    def test_non_bosch_entity_passes_through(self):
        """Other camera integrations on the same logger must not be
        affected — their entity_ids don't start with 'bosch_'."""
        from custom_components.bosch_shc_camera import _StreamSupportNoiseFilter
        f = _StreamSupportNoiseFilter()
        msg = ("Error requesting stream: camera.frigate_garden "
               "does not support play stream service")
        assert f.filter(self._record(msg)) is True
        # Even the second time
        assert f.filter(self._record(msg)) is True

    def test_different_entities_tracked_independently(self):
        from custom_components.bosch_shc_camera import _StreamSupportNoiseFilter
        f = _StreamSupportNoiseFilter()
        m1 = ("Error requesting stream: camera.bosch_terrasse "
              "does not support play stream service")
        m2 = ("Error requesting stream: camera.bosch_kamera "
              "does not support play stream service")
        # Both first occurrences pass
        assert f.filter(self._record(m1)) is True
        assert f.filter(self._record(m2)) is True
        # Both seconds dropped
        assert f.filter(self._record(m1)) is False
        assert f.filter(self._record(m2)) is False

    def test_max_tracked_prunes_oldest(self):
        """The dict caps at `_MAX_TRACKED` entries to prevent unbounded
        growth. When full, the oldest entry is evicted to make room for
        the new one. Pin the cap so a future refactor can't silently
        remove the prune (memory leak in long-running HA installs)."""
        from custom_components.bosch_shc_camera import _StreamSupportNoiseFilter
        f = _StreamSupportNoiseFilter()
        # Fill past the limit
        for i in range(f._MAX_TRACKED + 5):
            msg = (f"Error requesting stream: camera.bosch_test_{i:03d} "
                   "does not support play stream service")
            f.filter(self._record(msg))
        assert len(f._last_passed) <= f._MAX_TRACKED, (
            f"Filter dict grew past _MAX_TRACKED ({f._MAX_TRACKED}) — "
            "prune logic broken, memory leak in long-running HA."
        )

    def test_install_idempotent(self):
        """Re-installing the filter must not stack duplicates."""
        from custom_components.bosch_shc_camera import (
            _StreamSupportNoiseFilter,
            _install_stream_support_noise_filter,
        )
        cam_logger = logging.getLogger("homeassistant.components.camera")
        # Clear any prior state
        for f in list(cam_logger.filters):
            if isinstance(f, _StreamSupportNoiseFilter):
                cam_logger.removeFilter(f)
        _install_stream_support_noise_filter()
        _install_stream_support_noise_filter()
        _install_stream_support_noise_filter()
        count = sum(
            1 for f in cam_logger.filters
            if isinstance(f, _StreamSupportNoiseFilter)
        )
        assert count == 1, f"Expected 1 filter, got {count}"
        # Cleanup so other tests don't see this filter
        for f in list(cam_logger.filters):
            if isinstance(f, _StreamSupportNoiseFilter):
                cam_logger.removeFilter(f)


# ── _StreamWorkerErrorListener ────────────────────────────────────────────


class TestStreamWorkerErrorListener:
    """The log handler that bridges HA's stream worker errors to the
    coordinator's per-camera error counter. v10.3.3 — fixes the
    yellow→blue→yellow cycle that polling alone missed."""

    def _coord_with_entity(self, entity_id: str = "camera.bosch_terrasse"):
        cam_entity = SimpleNamespace(entity_id=entity_id)
        coord = SimpleNamespace(
            _camera_entities={CAM_A: cam_entity},
            hass=SimpleNamespace(
                loop=SimpleNamespace(call_soon_threadsafe=MagicMock()),
            ),
            _schedule_stream_worker_error=MagicMock(),
        )
        return coord

    def _record(self, name: str, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name=name,
            level=logging.ERROR,
            pathname="x",
            lineno=1,
            msg=msg,
            args=None,
            exc_info=None,
        )

    def test_below_error_level_ignored(self):
        from custom_components.bosch_shc_camera import _StreamWorkerErrorListener
        coord = self._coord_with_entity()
        listener = _StreamWorkerErrorListener(coord)
        rec = logging.LogRecord(
            name="homeassistant.components.stream.stream.camera.bosch_terrasse",
            level=logging.WARNING,
            pathname="x", lineno=1,
            msg="Error from stream worker", args=None, exc_info=None,
        )
        listener.emit(rec)
        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_unrelated_error_ignored(self):
        from custom_components.bosch_shc_camera import _StreamWorkerErrorListener
        coord = self._coord_with_entity()
        listener = _StreamWorkerErrorListener(coord)
        listener.emit(self._record(
            "homeassistant.components.stream.stream.camera.bosch_terrasse",
            "RecorderBuildError: foo",
        ))
        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_unrelated_logger_ignored(self):
        """Stream worker errors on other loggers (or with the marker
        substring outside the right place) must not trigger."""
        from custom_components.bosch_shc_camera import _StreamWorkerErrorListener
        coord = self._coord_with_entity()
        listener = _StreamWorkerErrorListener(coord)
        listener.emit(self._record(
            "homeassistant.components.camera",  # wrong logger
            "Error from stream worker for some other camera",
        ))
        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_unknown_entity_ignored(self):
        """Logger hits the marker, but the entity_id doesn't map to any
        of our cam_ids — must skip silently rather than crash."""
        from custom_components.bosch_shc_camera import _StreamWorkerErrorListener
        coord = self._coord_with_entity()
        listener = _StreamWorkerErrorListener(coord)
        listener.emit(self._record(
            "homeassistant.components.stream.stream.camera.frigate_garden",
            "Error from stream worker",
        ))
        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_match_routes_to_coordinator(self):
        from custom_components.bosch_shc_camera import _StreamWorkerErrorListener
        coord = self._coord_with_entity()
        listener = _StreamWorkerErrorListener(coord)
        listener.emit(self._record(
            "homeassistant.components.stream.stream.camera.bosch_terrasse",
            "Error from stream worker: TimeoutError",
        ))
        coord.hass.loop.call_soon_threadsafe.assert_called_once()
        call_args = coord.hass.loop.call_soon_threadsafe.call_args[0]
        # First positional arg is the function, then cam_id, msg.
        assert call_args[0] is coord._schedule_stream_worker_error
        assert call_args[1] == CAM_A
        assert "TimeoutError" in call_args[2]

    def test_handler_swallows_exceptions(self):
        """The handler runs inside `logging.emit` — ANY exception here
        would be routed back to logging's error handler and could spam
        the log with a re-entrant feedback loop. The except clause must
        swallow everything."""
        from custom_components.bosch_shc_camera import _StreamWorkerErrorListener
        # Coordinator missing _camera_entities — would normally AttributeError.
        broken_coord = SimpleNamespace()  # no _camera_entities, no hass
        listener = _StreamWorkerErrorListener(broken_coord)
        # Must NOT raise
        listener.emit(self._record(
            "homeassistant.components.stream.stream.camera.bosch_terrasse",
            "Error from stream worker",
        ))


# ── is_camera_online / is_session_stale ───────────────────────────────────


class TestStateChecks:
    """Trivial state reads, but pinned because every entity's `available`
    property routes through them. A regression in either silently breaks
    the entire entity availability layer."""

    def test_is_camera_online_true_when_online(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(data={CAM_A: {"status": "ONLINE"}})
        assert BoschCameraCoordinator.is_camera_online(coord, CAM_A) is True

    def test_is_camera_online_false_when_offline(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(data={CAM_A: {"status": "OFFLINE"}})
        assert BoschCameraCoordinator.is_camera_online(coord, CAM_A) is False

    def test_is_camera_online_false_when_status_missing(self):
        """Camera with no status field must default to NOT online (UNKNOWN
        != ONLINE) — better to gate the switch off than to fire commands
        at a possibly-offline cam."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(data={CAM_A: {}})
        assert BoschCameraCoordinator.is_camera_online(coord, CAM_A) is False

    def test_is_camera_online_false_when_cam_unknown(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(data={})
        assert BoschCameraCoordinator.is_camera_online(coord, CAM_A) is False

    def test_is_session_stale_default_false(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(_session_stale={})
        assert BoschCameraCoordinator.is_session_stale(coord, CAM_A) is False

    def test_is_session_stale_true_after_3_failures(self):
        """The flag is set elsewhere by the auto-renew loop; here we
        just pin the read contract."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(_session_stale={CAM_A: True})
        assert BoschCameraCoordinator.is_session_stale(coord, CAM_A) is True


# ── _refresh_local_creds_from_heartbeat ────────────────────────────────────


class TestRefreshLocalCredsFromHeartbeat:
    """Heartbeat cred-refresh — v10.4.10 fix #1.

    Bosch's PUT /v11/video_inputs/{id}/connection LOCAL returns a fresh
    digest user/password pair on every call. The old pair stops accepting
    NEW RTSP connects within ~60 s. Without this refresh, the next
    stream-worker reconnect after idle gets HTTP 401 and trips
    LOCAL→REMOTE fallback. Capture analysis in `captures/api-findings.md`
    §1 confirmed this happens every ~333 s on Gen2 Outdoor.

    This handler runs inside the heartbeat loop; it MUST NOT crash on
    bad input (bad JSON, missing creds, missing live session) — the
    heartbeat must keep going regardless.
    """

    def _coord(self, **overrides):
        cam_entity = SimpleNamespace(stream=None)
        base = dict(
            _live_connections={
                CAM_A: {
                    "_connection_type": "LOCAL",
                    "_local_user": "old-user",
                    "_local_password": "old-pass",
                    "rtspsUrl": "rtsp://old-user:old-pass@127.0.0.1:46767/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=3600",
                    "rtspUrl": "rtsp://old-user:old-pass@127.0.0.1:46767/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=3600",
                },
            },
            _tls_proxy_ports={CAM_A: 46767},
            _local_creds_cache={},
            _audio_enabled={},
            _hw_version={CAM_A: "CAMERA_EYES"},
            _camera_entities={CAM_A: cam_entity},
            _nvr_processes={},
            _nvr_user_intent={},
            hass=SimpleNamespace(async_create_task=MagicMock()),
            debug=False,
            get_model_config=lambda cid: SimpleNamespace(max_session_duration=3600),
        )
        base.update(overrides)
        return SimpleNamespace(**base), cam_entity

    def test_happy_path_updates_creds_and_url(self):
        """Fresh user+password in response → live dict + cache + URL all
        updated. The new URL must point to the same proxy port and carry
        the new creds."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord()
        resp = '{"user": "new-user", "password": "new-pass"}'
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, resp, generation=1, elapsed=30.0,
        )
        live = coord._live_connections[CAM_A]
        assert live["_local_user"] == "new-user"
        assert live["_local_password"] == "new-pass"
        assert "new-user" in live["rtspsUrl"]
        assert "new-pass" in live["rtspsUrl"]
        assert "127.0.0.1:46767" in live["rtspsUrl"]
        # Cache also updated
        assert coord._local_creds_cache[CAM_A]["user"] == "new-user"
        assert coord._local_creds_cache[CAM_A]["password"] == "new-pass"

    def test_url_keeps_inst_param(self):
        """The original rtspsUrl carries `inst=1` — the rebuilt URL must
        preserve it. Bosch's session-per-instance limits mean wrong inst
        triggers concurrent-session rejection on Gen1."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord()
        # Override URL with inst=2
        coord._live_connections[CAM_A]["rtspsUrl"] = (
            "rtsp://old:old@127.0.0.1:46767/rtsp_tunnel?inst=2&enableaudio=1"
        )
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, '{"user": "n", "password": "p"}',
            generation=1, elapsed=10.0,
        )
        assert "inst=2" in coord._live_connections[CAM_A]["rtspsUrl"]

    def test_audio_disabled_drops_enableaudio(self):
        """When audio is off (snapshot-only mode), the rebuilt URL must
        NOT include `enableaudio=1` — including it pulls audio packets
        and wastes LAN bandwidth."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord(_audio_enabled={CAM_A: False})
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, '{"user": "n", "password": "p"}',
            generation=1, elapsed=10.0,
        )
        assert "enableaudio" not in coord._live_connections[CAM_A]["rtspsUrl"]

    def test_no_creds_in_response_skips_silently(self):
        """Bosch sometimes returns {} on heartbeat — must be a no-op."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord()
        before = dict(coord._live_connections[CAM_A])
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, "{}", generation=1, elapsed=10.0,
        )
        assert coord._live_connections[CAM_A] == before

    def test_session_torn_down_skips(self):
        """If the live session was torn down between PUT and parse, the
        cam_id is gone from `_live_connections` — must not crash."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord(_live_connections={})
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, '{"user": "n", "password": "p"}',
            generation=1, elapsed=10.0,
        )
        # No exception, no mutation
        assert coord._live_connections == {}

    def test_session_now_remote_skips(self):
        """If LOCAL fell back to REMOTE in the last second, the LOCAL
        creds are no longer relevant. Skip rather than overwrite a
        REMOTE session with rebuilt LOCAL URL."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord()
        coord._live_connections[CAM_A]["_connection_type"] = "REMOTE"
        before = dict(coord._live_connections[CAM_A])
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, '{"user": "n", "password": "p"}',
            generation=1, elapsed=10.0,
        )
        assert coord._live_connections[CAM_A]["_connection_type"] == "REMOTE"
        # creds untouched
        assert coord._live_connections[CAM_A]["_local_user"] == before["_local_user"]

    def test_unchanged_creds_skip(self):
        """If the response carries the same user+password we already
        have, skip — no need to call Stream.update_source for nothing."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord()
        # Stream stub that records calls
        stream = MagicMock()
        coord._camera_entities[CAM_A].stream = stream
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A,
            '{"user": "old-user", "password": "old-pass"}',
            generation=1, elapsed=10.0,
        )
        stream.update_source.assert_not_called()

    def test_stream_update_source_called(self):
        """When creds change AND the camera entity has a Stream object,
        Stream.update_source must be called with the new URL so HA's
        stream worker rebuilds without a teardown."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord()
        stream = MagicMock()
        coord._camera_entities[CAM_A].stream = stream
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A,
            '{"user": "n", "password": "p"}',
            generation=1, elapsed=10.0,
        )
        stream.update_source.assert_called_once()
        call_url = stream.update_source.call_args[0][0]
        assert "n:p@127.0.0.1:46767" in call_url

    def test_stream_update_source_swallows_exceptions(self):
        """If `Stream.update_source` raises (HA bug, race), the helper
        must keep going and update the cache anyway — the next worker
        restart will pick up the cached URL."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord()
        stream = MagicMock()
        stream.update_source.side_effect = RuntimeError("HA stream error")
        coord._camera_entities[CAM_A].stream = stream
        # Must NOT raise
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A,
            '{"user": "n", "password": "p"}',
            generation=1, elapsed=10.0,
        )
        # Cache still updated
        assert coord._local_creds_cache[CAM_A]["user"] == "n"

    def test_no_proxy_port_skips(self):
        """If TLS proxy was stopped between PUT and parse, no port to
        point the URL at — must skip, not crash."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord(_tls_proxy_ports={})
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, '{"user": "n", "password": "p"}',
            generation=1, elapsed=10.0,
        )
        # Live conn untouched
        assert coord._live_connections[CAM_A]["_local_user"] == "old-user"

    def test_bad_json_swallowed(self):
        """The handler is best-effort — bad JSON must not crash the
        heartbeat loop. The reactive 401-rescue path is the safety net."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord()
        # Must NOT raise
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, "not json {{{ bad", generation=1, elapsed=10.0,
        )

    def test_active_recorder_triggers_restart(self):
        """When the NVR is recording for this cam, the cred change
        kills the ffmpeg sidecar within ~60 s. Re-spawn it now (with
        the new URL) — the ~1-2 s gap is documented in mini-nvr-concept.
        Pin so a refactor of the NVR teardown can't silently drop this."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = self._coord(
            _nvr_processes={CAM_A: object()},   # any non-empty value
            _nvr_user_intent={CAM_A: True},
        )
        coord._restart_recorder_if_active = MagicMock()
        BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
            coord, CAM_A, '{"user": "n", "password": "p"}',
            generation=1, elapsed=10.0,
        )
        coord.hass.async_create_task.assert_called_once()


# ── _FCMNoiseFilter (in fcm.py) ────────────────────────────────────────────


class TestFCMNoiseFilter:
    """v10.4.10 fix #2 — strips the recursive trace from
    firebase_messaging's "Unexpected exception during read" record and
    rate-limits to 1 line/60 s. Without this filter, a router reboot
    triggers ~200 lines/s of multi-thousand-line stacks until WAN comes
    back (real incident 2026-04-28: HA CPU 30 % → 85 %, coordinator
    stalled 4 min)."""

    def _record(self, msg: str, exc_text: str = "fake-trace") -> logging.LogRecord:
        rec = logging.LogRecord(
            name="firebase_messaging.fcmpushclient",
            level=logging.ERROR,
            pathname="x", lineno=1,
            msg=msg, args=None, exc_info=None,
        )
        rec.exc_text = exc_text
        return rec

    def test_unrelated_message_passes(self):
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        rec = self._record("Connected to FCM")
        assert f.filter(rec) is True
        # Trace not stripped
        assert rec.exc_text == "fake-trace"

    def test_target_record_first_passes_but_strips_trace(self):
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        rec = self._record("Unexpected exception during read")
        assert f.filter(rec) is True
        # Trace stripped — this is the load-bearing assertion
        assert rec.exc_info is None
        assert rec.exc_text is None

    def test_second_within_60s_dropped(self):
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        f.filter(self._record("Unexpected exception during read"))
        rec2 = self._record("Unexpected exception during read")
        assert f.filter(rec2) is False
        # Trace still stripped on dropped record (defensive — record may
        # land in another handler that doesn't use this filter).
        assert rec2.exc_info is None

    def test_install_idempotent(self):
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )
        fcm_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        # Clear prior state
        for f in list(fcm_logger.filters):
            if isinstance(f, _FCMNoiseFilter):
                fcm_logger.removeFilter(f)
        _install_fcm_noise_filter()
        _install_fcm_noise_filter()
        _install_fcm_noise_filter()
        count = sum(
            1 for f in fcm_logger.filters
            if isinstance(f, _FCMNoiseFilter)
        )
        assert count == 1
        # Cleanup
        for f in list(fcm_logger.filters):
            if isinstance(f, _FCMNoiseFilter):
                fcm_logger.removeFilter(f)


# ── Module-level constant pinning ────────────────────────────────────────


class TestModuleConstants:
    """Constants whose value matters for behaviour."""

    def test_safe_domains_locked(self):
        """The SSRF allowlist must stay narrow. Adding e.g.
        `.example.com` here would silently widen the surface."""
        from custom_components.bosch_shc_camera import _SAFE_DOMAINS
        assert _SAFE_DOMAINS == frozenset({".boschsecurity.com", ".bosch.com"}), (
            f"_SAFE_DOMAINS changed to {_SAFE_DOMAINS} — only Bosch domains "
            "should be reachable via the resolver."
        )

    def test_safe_domains_have_leading_dot(self):
        """Each entry must start with `.` so `endswith` rejects
        lookalikes like `attacker-bosch.com`."""
        from custom_components.bosch_shc_camera import _SAFE_DOMAINS
        for d in _SAFE_DOMAINS:
            assert d.startswith("."), (
                f"_SAFE_DOMAINS entry {d!r} missing leading '.' — "
                "endswith('boschsecurity.com') would also match "
                "'attacker-boschsecurity.com'."
            )

    def test_integration_version_string_matches_manifest(self):
        """`_INTEGRATION_VERSION` must match `manifest.json` so
        diagnostics + service-call metadata report a coherent version."""
        import json
        from pathlib import Path
        from custom_components.bosch_shc_camera import _INTEGRATION_VERSION
        manifest = json.loads(
            (Path(__file__).parent.parent
             / "custom_components" / "bosch_shc_camera" / "manifest.json"
             ).read_text()
        )
        assert _INTEGRATION_VERSION == manifest["version"]
