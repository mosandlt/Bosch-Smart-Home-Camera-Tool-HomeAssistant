"""Tests for `BoschCameraCoordinator.__init__` body (lines 273-611).

This is the load-bearing constructor — it allocates every per-camera and
per-coordinator state container the rest of the integration relies on
(caches, locks, write-lock timestamps, NVR state, FCM state, TLS proxy
ports, stream-error counters, ...).

The body is ~340 statements and otherwise un-tested (other tests in
`test_init_*` exercise *methods* on stub coordinators, not the constructor
itself). A regression here — e.g. someone deleting an attribute init or
flipping a default from `float('-inf')` to `0.0` (which would break
SENTINEL_RULE: CI VMs boot with monotonic ~200s, so `now - 0.0` is always
larger than any interval) — silently degrades stream/FCM/NVR behaviour.

Tests use a real `MockConfigEntry` from pytest_homeassistant_custom_component
+ the real `hass` fixture so we drive the actual `DataUpdateCoordinator.__init__`
chain. No MagicMock subclassing of the coordinator — every attribute assignment
in the body actually runs.

Coverage targets:
  - All ~120 attribute assignments in __init__ run on a minimal config entry
  - SENTINEL_RULE: every monotonic-comparison default is `float('-inf')`,
    never `0.0` (CI-VM-safe)
  - Options vs. data path: scan_interval from options drives update_interval
  - Empty options falls back to DEFAULT_OPTIONS (scan_interval=60)
  - get_model_config() returns the right CameraModelConfig per hw_version
    (Gen1 indoor/outdoor + Gen2 indoor/outdoor)
  - Backwards-compat: old config-entry shape (no options, no NVR fields)
    instantiates cleanly with safe defaults
"""
from __future__ import annotations

import math
import time

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.const import DOMAIN


# ─── Helpers ───────────────────────────────────────────────────────────────


def _make_entry(
    hass: HomeAssistant,
    *,
    data: dict | None = None,
    options: dict | None = None,
) -> MockConfigEntry:
    """Build a MockConfigEntry attached to hass — usable by the coordinator."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Bosch Smart Home Camera",
        data=data if data is not None else {
            "bearer_token": "test_bearer_token",
            "refresh_token": "test_refresh_token",
        },
        options=options if options is not None else {},
        unique_id=DOMAIN,
        version=1,
    )
    entry.add_to_hass(hass)
    return entry


# ─── Basic instantiation ───────────────────────────────────────────────────


async def test_coordinator_instantiates_with_minimal_entry(hass: HomeAssistant) -> None:
    """Default config entry → coordinator builds, _entry stored, DataUpdateCoordinator
    parent ctor ran (update_interval, hass, name)."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    assert coord._entry is entry
    assert coord.hass is hass
    assert coord.name == DOMAIN
    # DataUpdateCoordinator parent ctor set update_interval from DEFAULT_OPTIONS scan_interval=60.
    assert coord.update_interval is not None
    assert coord.update_interval.total_seconds() == 60.0


async def test_options_snapshot_is_deep_enough_copy(hass: HomeAssistant) -> None:
    """`_options_snapshot` must NOT alias entry.options — later mutations of
    entry.options must not silently change the snapshot. Used by
    `_async_options_updated` to detect real edits vs. token-refresh
    data-only updates."""
    entry = _make_entry(hass, options={"scan_interval": 90})
    coord = BoschCameraCoordinator(hass, entry)
    # Snapshot reflects merged options (DEFAULT_OPTIONS + entry.options)
    assert coord._options_snapshot["scan_interval"] == 90
    # Mutating the live entry.options must not bleed into the snapshot.
    # MockConfigEntry.options is a dict — we mutate via hass API.
    hass.config_entries.async_update_entry(entry, options={"scan_interval": 30})
    # Snapshot stays at 90.
    assert coord._options_snapshot["scan_interval"] == 90


async def test_scan_interval_from_options_drives_update_interval(hass: HomeAssistant) -> None:
    """options.scan_interval overrides DEFAULT_OPTIONS."""
    entry = _make_entry(hass, options={"scan_interval": 30})
    coord = BoschCameraCoordinator(hass, entry)
    assert coord.update_interval.total_seconds() == 30.0


async def test_scan_interval_falls_back_to_default_when_options_empty(
    hass: HomeAssistant,
) -> None:
    """Empty options → DEFAULT_OPTIONS scan_interval=60 wins."""
    entry = _make_entry(hass, options={})
    coord = BoschCameraCoordinator(hass, entry)
    assert coord.update_interval.total_seconds() == 60.0


# ─── SENTINEL_RULE — float('-inf') for "never done" timestamps ──────────────


async def test_monotonic_sentinels_use_negative_infinity(hass: HomeAssistant) -> None:
    """Every default that participates in `time.monotonic() - x >= interval`
    must be `float('-inf')`, NEVER `0.0`. CI VMs boot fresh; with `0.0`
    a first-tick check `monotonic()(~200s) - 0.0 >= scan_interval(60)` is
    True by accident, masking initialisation bugs. With `-inf` it's True
    by *intent* — that's the SENTINEL_RULE contract."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    assert coord._last_smb_cleanup == -math.inf, "_last_smb_cleanup default must be -inf"
    assert coord._last_nvr_cleanup == -math.inf, "_last_nvr_cleanup default must be -inf"


async def test_per_type_last_fetched_defaults_are_large_negative(
    hass: HomeAssistant,
) -> None:
    """`_last_status` / `_last_events` / `_last_slow` / `_last_lighting_switch`
    use `-86400.0` (rather than -inf) — far enough in the past that the
    first tick always fetches. Both -inf and -86400 satisfy the
    "first-tick always fires" contract; the file uses -86400 here so pin
    that exact value to flag accidental zero-init."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    for attr in (
        "_last_status",
        "_last_events",
        "_last_slow",
        "_last_lighting_switch",
    ):
        v = getattr(coord, attr)
        assert v <= -3600.0, (
            f"{attr}={v!r} — too close to 0; first-tick fetch may be masked on a fresh CI VM"
        )


# ─── Every documented attribute is initialised (catches accidental deletions) ─


async def test_all_documented_state_containers_initialised(hass: HomeAssistant) -> None:
    """Spot-check that the big state containers from __init__ exist with the
    documented type. The point is to catch the regression "someone deleted
    `self._foo: dict = {}` and nothing else broke yet" — many of these
    dicts are written from background tasks long after setup."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    # ── Live-stream & sessions ─────────────────────────────────────────
    assert coord._live_connections == {}
    assert coord._live_opened_at == {}
    assert coord._rcp_state_cache == {}
    assert coord._stream_type_override is None
    assert coord._audio_enabled == {}

    # ── Renewal/task tracking ──────────────────────────────────────────
    assert coord._auto_renew_tasks == {}
    assert coord._renewal_tasks == {}
    assert coord._auto_renew_generation == {}
    assert coord._camera_entities == {}

    # ── Cached data tiers ──────────────────────────────────────────────
    assert coord._cached_status == {}
    assert coord._cached_events == {}
    assert coord._shc_state_cache == {}
    assert coord._shc_devices_raw == []

    # ── SHC health tracking ────────────────────────────────────────────
    assert coord._shc_available is True
    assert coord._shc_fail_count == 0
    assert coord._SHC_MAX_FAILS == 3
    assert coord._SHC_RETRY_INTERVAL == 120

    # ── Per-camera caches ──────────────────────────────────────────────
    assert coord._pan_cache == {}
    assert coord._wifiinfo_cache == {}
    assert coord._ambient_light_cache == {}
    assert coord._rcp_dimmer_cache == {}
    assert coord._rcp_privacy_cache == {}
    assert coord._rcp_clock_offset_cache == {}
    assert coord._rcp_lan_ip_cache == {}
    assert coord._rcp_product_name_cache == {}
    assert coord._rcp_bitrate_cache == {}

    # ── Phase-2 RCP caches ─────────────────────────────────────────────
    assert coord._rcp_alarm_catalog_cache == {}
    assert coord._rcp_motion_zones_cache == {}
    assert coord._rcp_motion_coords_cache == {}
    assert coord._rcp_tls_cert_cache == {}
    assert coord._rcp_network_services_cache == {}
    assert coord._rcp_iva_catalog_cache == {}
    assert coord._rcp_cmd_failures == {}
    assert coord._quality_preference == {}
    assert coord._rcp_session_cache == {}
    assert coord._proxy_url_cache == {}

    # ── Locks (created eagerly, used lazily) ───────────────────────────
    assert coord._snapshot_fetch_locks == {}
    assert coord._stream_locks == {}
    assert coord._fresh_snap_cache == {}
    assert coord._fresh_snap_locks == {}

    # ── FCM ─────────────────────────────────────────────────────────────
    assert coord._fcm_client is None
    assert coord._fcm_token == ""
    assert coord._fcm_running is False
    assert coord._fcm_last_push == float("-inf")
    assert coord._fcm_healthy is False
    assert coord._fcm_push_mode == "unknown"
    # Lock has to be an actual threading.Lock so cross-thread writes are safe.
    import threading
    assert isinstance(coord._fcm_lock, threading.Lock().__class__)

    # ── Misc caches ────────────────────────────────────────────────────
    assert coord._unread_events_cache == {}
    assert coord._privacy_sound_cache == {}
    assert coord._commissioned_cache == {}
    assert coord._feature_flags == {}
    assert coord._protocol_checked is False
    assert isinstance(coord._integration_version, str)
    assert coord._firmware_cache == {}

    # ── Token-refresh state ────────────────────────────────────────────
    assert coord._token_alert_sent is False
    assert coord._token_fail_count == 0
    assert coord._auth_outage_count == 0
    assert coord._auth_outage_alert_sent is False
    assert coord._auth_outage_next_retry_ts == 0.0
    assert coord._local_creds_cache == {}
    # Refresh lock must be an asyncio.Lock so concurrent refreshes serialize.
    import asyncio as _asyncio
    assert isinstance(coord._token_refresh_lock, _asyncio.Lock)
    assert coord._token_refresh_handle is None
    assert coord._bg_tasks == set()

    # ── Session-stale flag + write locks ───────────────────────────────
    assert coord._session_stale == {}
    assert coord._timestamp_cache == {}
    assert coord._ledlights_cache == {}
    assert coord._lens_elevation_cache == {}
    assert coord._audio_cache == {}
    assert coord._motion_light_cache == {}
    assert coord._image_rotation_180 == {}
    assert coord._ambient_lighting_cache == {}
    assert coord._lighting_switch_cache == {}
    assert coord._global_lighting_cache == {}
    assert coord._notifications_cache == {}
    assert coord._rules_cache == {}
    assert coord._cloud_zones_cache == {}
    assert coord._cloud_privacy_masks_cache == {}
    assert coord._lighting_options_cache == {}
    assert coord._intrusion_config_cache == {}
    assert coord._alarm_settings_cache == {}
    assert coord._audio_alarm_cache == {}
    assert coord._alarm_status_cache == {}
    assert coord._arming_cache == {}
    assert coord._icon_led_brightness_cache == {}
    assert coord._gen2_zones_cache == {}
    assert coord._gen2_private_areas_cache == {}
    assert coord._user_token_cache == {}

    # ── Write-lock timestamps ──────────────────────────────────────────
    for attr in (
        "_light_set_at",
        "_notif_set_at",
        "_privacy_set_at",
        "_privacy_sound_set_at",
        "_timestamp_set_at",
        "_ledlights_set_at",
        "_arming_set_at",
        "_audio_alarm_set_at",
    ):
        assert getattr(coord, attr) == {}, f"{attr} must default to {{}}"
    assert coord._WRITE_LOCK_SECS == 30.0
    assert coord._hw_version == {}

    # ── TLS proxy state ────────────────────────────────────────────────
    assert coord._tls_proxy_ports == {}
    assert coord._stream_error_count == {}
    assert coord._stream_error_at == {}
    assert coord._stream_fell_back == {}
    assert coord._local_rescue_attempts == {}
    assert coord._local_rescue_at == {}
    assert coord._lan_tcp_reachable == {}
    assert coord._local_promote_at == {}
    assert coord._tls_ssl_ctx is None

    # ── Offline tracking ───────────────────────────────────────────────
    assert coord._offline_since == {}
    assert coord._OFFLINE_EXTENDED_INTERVAL == 900
    assert coord._per_cam_status_at == {}
    assert coord._stream_warming == set()
    assert coord._stream_warming_started == {}

    # ── Mini-NVR state ─────────────────────────────────────────────────
    assert coord._nvr_processes == {}
    assert coord._nvr_user_intent == {}
    assert coord._nvr_error_state == {}
    assert coord._nvr_recent_crash == {}
    assert coord._nvr_preroll_processes == {}
    assert coord._nvr_preroll_last_crash == {}
    assert coord._nvr_drain_state == {}
    assert coord._nvr_drain_failures == {}
    assert coord._nvr_drain_task is None

    # ── Event-id tracking ──────────────────────────────────────────────
    assert coord._last_event_ids == {}
    # _download_started_at is set to wall-clock time.time(); allow ± 5s slack.
    assert abs(coord._download_started_at - time.time()) < 5.0
    assert coord._alert_sent_ids == {}


# ─── Options vs. data path ─────────────────────────────────────────────────


async def test_options_path_overrides_default_options(hass: HomeAssistant) -> None:
    """A handful of opt keys: snapshot, NVR — must propagate via
    `get_options(entry)` (DEFAULT_OPTIONS merged with entry.options)."""
    entry = _make_entry(
        hass,
        options={
            "scan_interval": 45,
            "enable_nvr": True,
            "nvr_retention_days": 7,
        },
    )
    coord = BoschCameraCoordinator(hass, entry)
    # Snapshot captures the merge result, not the raw entry.options.
    snap = coord._options_snapshot
    assert snap["scan_interval"] == 45
    assert snap["enable_nvr"] is True
    assert snap["nvr_retention_days"] == 7
    # DEFAULT_OPTIONS values still present for unspecified keys.
    assert snap["interval_status"] == 300
    assert snap["enable_snapshots"] is True


async def test_data_only_entry_does_not_break_init(hass: HomeAssistant) -> None:
    """Backwards-compat: old entries created before options-flow existed
    still load — empty options must yield the defaults, not crash."""
    entry = _make_entry(hass, options={})
    coord = BoschCameraCoordinator(hass, entry)
    # All NVR fields default to disabled / safe values
    assert coord._options_snapshot["enable_nvr"] is False
    assert coord._options_snapshot["nvr_storage_target"] == "local"


# ─── get_model_config — Gen1/Gen2 indoor/outdoor mapping ───────────────────


async def test_get_model_config_returns_gen1_indoor(hass: HomeAssistant) -> None:
    """CAMERA_360 / INDOOR → Gen1 indoor profile (heartbeat=30, snapshot_warmup=3)."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    coord._hw_version["cam-indoor-gen1"] = "CAMERA_360"
    cfg = coord.get_model_config("cam-indoor-gen1")
    assert cfg.generation == 1
    assert cfg.heartbeat_interval == 30
    assert cfg.snapshot_warmup == 3
    assert cfg.display_name == "360 Innenkamera"


async def test_get_model_config_returns_gen1_outdoor(hass: HomeAssistant) -> None:
    """CAMERA_EYES / OUTDOOR → Gen1 outdoor profile (heartbeat=10 aggressive,
    snapshot_warmup=5, max_stream_errors=10)."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    coord._hw_version["cam-outdoor-gen1"] = "CAMERA_EYES"
    cfg = coord.get_model_config("cam-outdoor-gen1")
    assert cfg.generation == 1
    assert cfg.heartbeat_interval == 10
    assert cfg.snapshot_warmup == 5
    assert cfg.max_stream_errors == 10
    assert cfg.display_name == "Eyes Außenkamera"


async def test_get_model_config_returns_gen2_outdoor(hass: HomeAssistant) -> None:
    """HOME_Eyes_Outdoor → Gen2 outdoor profile.
    heartbeat=3600 (disabled, see models.py docs — PUT /connection rotates
    LOCAL Digest creds and kills FFmpeg). FFmpeg GET_PARAMETER every 15s
    is the only keepalive that doesn't break the stream."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    coord._hw_version["cam-outdoor-gen2"] = "HOME_Eyes_Outdoor"
    cfg = coord.get_model_config("cam-outdoor-gen2")
    assert cfg.generation == 2
    assert cfg.heartbeat_interval == 3600
    assert cfg.max_stream_errors == 10
    assert cfg.display_name == "Eyes Außenkamera II"


async def test_get_model_config_returns_gen2_indoor(hass: HomeAssistant) -> None:
    """HOME_Eyes_Indoor → Gen2 indoor profile (heartbeat=3600, snapshot_warmup=3).

    Indoor FW 9.40.25 exhibits the same destructive PUT /connection behaviour
    as Outdoor — every heartbeat rotates Digest creds and tears the live RTSP
    session. heartbeat_interval=3600 disables the destructive ping; FFmpeg's
    own GET_PARAMETER every ~15s keeps the session alive.
    """
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    coord._hw_version["cam-indoor-gen2"] = "HOME_Eyes_Indoor"
    cfg = coord.get_model_config("cam-indoor-gen2")
    assert cfg.generation == 2
    assert cfg.heartbeat_interval == 3600
    assert cfg.renewal_interval == 3600
    assert cfg.snapshot_warmup == 3
    assert cfg.display_name == "Eyes Innenkamera II"


async def test_get_model_config_unknown_hw_falls_back_to_default(
    hass: HomeAssistant,
) -> None:
    """Unknown hardwareVersion → DEFAULT_MODEL (heartbeat=15, gen=1,
    display_name='Unknown Camera') — keeps the integration alive when
    Bosch ships a new firmware with a brand-new hw string."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    coord._hw_version["cam-future"] = "HOME_Eyes_Outdoor_III_FuturePro"
    cfg = coord.get_model_config("cam-future")
    assert cfg.display_name == "Unknown Camera"
    assert cfg.heartbeat_interval == 15


async def test_get_model_config_missing_hw_uses_default(hass: HomeAssistant) -> None:
    """No `_hw_version` entry for the cam → falls back via default 'CAMERA'
    string, which is not in MODELS → DEFAULT_MODEL."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    cfg = coord.get_model_config("never-registered-cam")
    assert cfg.display_name == "Unknown Camera"


# ─── Multiple coordinators don't share state (per-instance dicts) ──────────


async def test_two_coordinators_have_independent_state(hass: HomeAssistant) -> None:
    """Each coordinator must own its own state dicts — a regression where
    one of the assignments used a class-level default (e.g. `dict = {}` as
    a class attribute) would alias state across config entries."""
    entry1 = _make_entry(hass, options={"scan_interval": 30})
    entry2 = _make_entry(
        hass,
        data={"bearer_token": "other_bearer", "refresh_token": "other_refresh"},
        options={"scan_interval": 90},
    )
    # Two different entries → two coordinators.
    coord1 = BoschCameraCoordinator(hass, entry1)
    coord2 = BoschCameraCoordinator(hass, entry2)
    # Mutate one — the other must remain empty.
    coord1._tls_proxy_ports["cam-A"] = 8001
    coord1._stream_warming.add("cam-A")
    coord1._hw_version["cam-A"] = "HOME_Eyes_Outdoor"
    assert coord2._tls_proxy_ports == {}
    assert coord2._stream_warming == set()
    assert coord2._hw_version == {}
    # Update intervals differ per entry options.
    assert coord1.update_interval.total_seconds() == 30.0
    assert coord2.update_interval.total_seconds() == 90.0


