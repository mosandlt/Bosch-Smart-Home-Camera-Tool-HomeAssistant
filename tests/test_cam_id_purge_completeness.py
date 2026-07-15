"""Completeness tests for `BoschCameraCoordinator._purge_cam_id` (Runde 2 P1 #1).

`cleanup_stale_devices` previously only removed the device-registry entry
for a camera no longer present in the Bosch cloud account — none of the
~100+ per-cam_id-keyed coordinator dict/set attributes were ever cleared, so
they grew unbounded across camera swaps/renames over the coordinator
instance's lifetime.

This test builds a REAL coordinator (via `BoschCameraCoordinator(hass, entry)`,
not a stub), populates every per-cam_id dict/set attribute discovered via
`vars(coordinator)` with a known test cam_id, calls `_purge_cam_id`, and then
asserts NONE of the expected-to-be-purged attributes still contain that
cam_id — while the deliberately-excluded attributes (proxy_hash-keyed,
event_id-keyed, account-level/global) are confirmed UNTOUCHED. A future new
per-cam dict/set that is not added to
`BoschCameraCoordinator._PURGE_CAM_DICT_ATTRS` /
`_PURGE_CAM_SET_ATTRS` will fail this test automatically once populated with
the test cam_id, because it is auto-discovered from `vars(coordinator)`
rather than hand-listed here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.const import DOMAIN

TEST_CAM_ID = "AABBCCDD-1111-2222-3333-444455556666"
OTHER_CAM_ID = "EEFF0011-9999-8888-7777-666655554444"
TEST_OPCODE = "0xABCD"

# Attributes that are intentionally NOT purged by `_purge_cam_id` — audited
# against `BoschCameraCoordinator.__init__` (see the comment block above
# `_PURGE_CAM_DICT_ATTRS` in __init__.py for the full rationale per entry).
# Each is populated with the test cam_id as a probe value and asserted to
# still contain it after `_purge_cam_id` runs, proving purge did NOT touch it.
EXCLUDED_STR_KEYED_DICTS = {
    # keyed by proxy_hash, not cam_id
    "rcp_session_cache",
    "rcp_session_locks",
    # keyed by event_id, not cam_id (pruned to 32 most recent separately)
    "alert_sent_ids",
    # account-level (GET /v11/feature_flags once), keyed by flag name
    "feature_flags",
    # coordinator-instance-level snapshot of options at creation time, not
    # keyed by cam_id at all (str keys just happen to be option names)
    "_options_snapshot",
    # inherited from HA-core's DataUpdateCoordinator base class — listener
    # callback registry, empty dict candidate but not ours / not cam-keyed
    "_listeners",
}

# Same idea for `set` attributes — sets of non-str members (e.g. Task
# objects) look identical to an empty `set[str]` candidate at construction
# time. Audited: `bg_tasks` holds `asyncio.Task` references, not cam_ids.
EXCLUDED_SETS = {
    "bg_tasks",
}

# Attribute handled specially: dict[tuple[str, str], float] keyed by
# (cam_id, opcode_hex) — not a plain str key, so it is excluded from the
# generic str-keyed dict/set scan below and tested explicitly.
TUPLE_KEYED_ATTR = "_rcp_lan_denied_until"


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Bosch Smart Home Camera",
        data={
            "bearer_token": "test_bearer_token",
            "refresh_token": "test_refresh_token",
        },
        options={},
        unique_id=DOMAIN,
        version=3,
    )
    entry.add_to_hass(hass)
    return entry


def _is_str_keyed_dict(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if not value:
        return True  # empty dict — assume str-keyed candidate, safe to probe
    return all(isinstance(k, str) for k in value)


def _is_str_set(value: object) -> bool:
    return isinstance(value, set)


async def test_purge_cam_id_completeness(hass: HomeAssistant) -> None:
    """Populate every per-cam_id dict/set with TEST_CAM_ID, purge, verify gone."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    purge_dict_attrs = set(coord._PURGE_CAM_DICT_ATTRS)
    purge_set_attrs = set(coord._PURGE_CAM_SET_ATTRS)

    # Discover every dict/set attribute on the real instance (auto-discovery
    # is the whole point — a future new per-cam dict gets caught here without
    # needing to be added to this test by hand).
    candidate_dict_attrs: list[str] = []
    candidate_set_attrs: list[str] = []
    for name, value in vars(coord).items():
        if name == TUPLE_KEYED_ATTR:
            continue
        if isinstance(value, dict) and _is_str_keyed_dict(value):
            candidate_dict_attrs.append(name)
        elif _is_str_set(value):
            candidate_set_attrs.append(name)

    # Sanity: every attribute this test intends to exercise must be either
    # a purge target or an audited exclusion — otherwise the test itself has
    # drifted from the source (e.g. a genuinely global dict added later with
    # str keys that happens not to be cam-keyed would need adding to
    # EXCLUDED_STR_KEYED_DICTS explicitly, forcing a conscious decision).
    unaccounted_dicts = (
        set(candidate_dict_attrs) - purge_dict_attrs - EXCLUDED_STR_KEYED_DICTS
    )
    unaccounted_sets = set(candidate_set_attrs) - purge_set_attrs - EXCLUDED_SETS
    assert unaccounted_dicts == set(), (
        f"New str-keyed dict attribute(s) {unaccounted_dicts} found on "
        "BoschCameraCoordinator that are neither in _PURGE_CAM_DICT_ATTRS "
        "nor in EXCLUDED_STR_KEYED_DICTS (tests/test_cam_id_purge_completeness.py) "
        "— audit whether they are keyed by cam_id and add them to one list "
        "or the other."
    )
    assert unaccounted_sets == set(), (
        f"New set attribute(s) {unaccounted_sets} found on "
        "BoschCameraCoordinator that are not in _PURGE_CAM_SET_ATTRS — audit "
        "whether they are cam_id-membership sets and add them there."
    )

    # Populate every discovered dict/set (both purge-targets and exclusions)
    # with the test cam_id as a probe.
    for name in candidate_dict_attrs:
        getattr(coord, name)[TEST_CAM_ID] = "probe"
    for name in candidate_set_attrs:
        getattr(coord, name).add(TEST_CAM_ID)
    # Tuple-keyed attr: one entry for the cam under test, one for a
    # different cam sharing the same opcode (must survive the purge).
    getattr(coord, TUPLE_KEYED_ATTR)[(TEST_CAM_ID, TEST_OPCODE)] = 123.0
    getattr(coord, TUPLE_KEYED_ATTR)[(OTHER_CAM_ID, TEST_OPCODE)] = 456.0

    coord._purge_cam_id(TEST_CAM_ID)

    # Every purge-target dict/set must no longer contain the test cam_id.
    still_present_dicts = [
        name for name in purge_dict_attrs if TEST_CAM_ID in getattr(coord, name)
    ]
    still_present_sets = [
        name for name in purge_set_attrs if TEST_CAM_ID in getattr(coord, name)
    ]
    assert still_present_dicts == [], (
        f"_purge_cam_id left TEST_CAM_ID behind in: {still_present_dicts}"
    )
    assert still_present_sets == [], (
        f"_purge_cam_id left TEST_CAM_ID behind in: {still_present_sets}"
    )

    # Excluded dicts must be UNTOUCHED (probe still present).
    for name in EXCLUDED_STR_KEYED_DICTS:
        assert TEST_CAM_ID in getattr(coord, name), (
            f"{name} is documented as excluded from cam_id purge but the "
            "probe entry was removed — either the exclusion is wrong or "
            "_purge_cam_id started touching it unexpectedly."
        )

    # Tuple-keyed attr: only the (TEST_CAM_ID, *) entry is purged.
    tuple_dict = getattr(coord, TUPLE_KEYED_ATTR)
    assert (TEST_CAM_ID, TEST_OPCODE) not in tuple_dict
    assert (OTHER_CAM_ID, TEST_OPCODE) in tuple_dict


async def test_purge_cam_id_representative_sample(hass: HomeAssistant) -> None:
    """Focused check on a representative sample of well-known per-cam caches."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.rcp_dimmer_cache[TEST_CAM_ID] = 42
    coord.shc_state_cache[TEST_CAM_ID] = {"privacy_mode": True}
    coord.pan_cache[TEST_CAM_ID] = 10
    coord.get_session(TEST_CAM_ID)  # populates coord._sessions[TEST_CAM_ID]
    assert TEST_CAM_ID in coord._sessions

    coord._purge_cam_id(TEST_CAM_ID)

    assert TEST_CAM_ID not in coord.rcp_dimmer_cache
    assert TEST_CAM_ID not in coord.shc_state_cache
    assert TEST_CAM_ID not in coord.pan_cache
    assert TEST_CAM_ID not in coord._sessions


async def test_purge_cam_id_all_slice2_cache_fields(hass: HomeAssistant) -> None:
    """All 27 Session-State-Facade Slice 2 `CacheFieldView` attributes purge.

    `test_purge_cam_id_completeness`'s `vars(coord)` auto-discovery cannot
    see these — they are `CacheFieldView` (a `MutableMapping`), not a bare
    `dict` instance, since Slice 2 (docs/stream-perf-stability-refactor-
    plan.md) backs them via `self._sessions`. Only 3 of the 27
    (`rcp_dimmer_cache`/`shc_state_cache`/`pan_cache`) had a dedicated
    regression check before this test (`test_purge_cam_id_representative_
    sample` below) — this covers the remaining 24 explicitly so a future
    change to `CacheFieldView`/`get_or_create_session` that broke purge for
    only some fields would be caught.
    """
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    slice2_probe_values: dict[str, object] = {
        "_rcp_state_cache": {"privacy_mode": False},
        "shc_state_cache": {"privacy_mode": True},
        "pan_cache": 10,
        "rcp_dimmer_cache": 42,
        "rcp_privacy_cache": 1,
        "rcp_clock_offset_cache": 1.5,
        "rcp_lan_ip_cache": "192.0.2.10",
        "rcp_product_name_cache": "Eyes Outdoor",
        "rcp_bitrate_cache": [512, 1024],
        "rcp_alarm_catalog_cache": [{"id": 1}],
        "rcp_motion_zones_cache": [{"id": 1}],
        "rcp_motion_coords_cache": [{"x": 1, "y": 1}],
        "rcp_tls_cert_cache": {"issuer": "Bosch"},
        "rcp_network_services_cache": ["rtsp"],
        "rcp_iva_catalog_cache": [{"id": 1}],
        "rcp_onvif_scopes_cache": {"name": "cam"},
        "rcp_version_cache": "1.2",
        "_nvr_mode_preference": "event_buffered",
        "local_creds_cache": {"user": "u", "password": "p"},
        "audio_cache": {"volume": 50},
        "nvr_user_intent": True,
        "nvr_error_state": "some error",
        "nvr_recent_crash": 123.0,
        "nvr_auth_retry_count": 2,
        "_nvr_event_clip_enabled": True,
        "_nvr_preroll_last_crash": 456.0,
        "nvr_preroll_segment_counts": 7,
    }

    for attr_name, probe_value in slice2_probe_values.items():
        getattr(coord, attr_name)[TEST_CAM_ID] = probe_value
        assert TEST_CAM_ID in getattr(coord, attr_name), (
            f"{attr_name} did not accept the probe write"
        )

    coord._purge_cam_id(TEST_CAM_ID)

    still_present = [
        attr_name
        for attr_name in slice2_probe_values
        if TEST_CAM_ID in getattr(coord, attr_name)
    ]
    assert still_present == [], (
        f"_purge_cam_id left TEST_CAM_ID behind in Slice 2 fields: {still_present}"
    )


async def test_purge_cam_id_slice3_session_stream_fields(hass: HomeAssistant) -> None:
    """`live_connections` (CacheFieldView) and `user_intent_streams`
    (BoolFieldView) — Session-State-Facade Slice 3 — purge correctly.

    Neither `test_purge_cam_id_completeness`'s `vars(coord)` auto-discovery
    (they are no longer bare `dict`/`set` instances) nor
    `test_purge_cam_id_all_slice2_cache_fields` (Slice 2 only) exercise
    these — same reasoning as the Slice 2 dedicated test above.
    """
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.live_connections[TEST_CAM_ID] = {"proxyUrl": "https://example.invalid"}
    coord.user_intent_streams.add(TEST_CAM_ID)
    assert TEST_CAM_ID in coord.live_connections
    assert TEST_CAM_ID in coord.user_intent_streams

    coord._purge_cam_id(TEST_CAM_ID)

    assert TEST_CAM_ID not in coord.live_connections
    assert TEST_CAM_ID not in coord.user_intent_streams


async def test_purge_cam_id_slice4_lock_fields(hass: HomeAssistant) -> None:
    """All five Session-State-Facade Slice 4 lock `CacheFieldView` attributes
    (`_stream_locks`/`_nvr_recorder_locks`/`_snapshot_fetch_locks`/
    `_nvr_clip_assembly_locks`/`_fresh_snap_locks`) purge correctly.
    (`_go2rtc_reregister_locks` was a sixth, removed 2026-07-14 along with
    the manual go2rtc PUT/DELETE registration it serialized —
    HA-Core-submission-prep.)

    Neither `test_purge_cam_id_completeness`'s `vars(coord)` auto-discovery
    (they are no longer bare `dict` instances) nor the Slice 2/3 dedicated
    tests exercise these — same reasoning as those tests. Also confirms the
    purge does not leave a dangling reference to a lock that might still be
    referenced (held) elsewhere — since `_purge_cam_id` only ever runs once
    a camera is confirmed gone from the Bosch cloud account (never
    mid-operation, per its own docstring), a fresh unlocked lock is
    populated here as the probe rather than a held one, matching real usage.
    """
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    lock_attrs = [
        "_stream_locks",
        "_nvr_recorder_locks",
        "_snapshot_fetch_locks",
        "_nvr_clip_assembly_locks",
        "_fresh_snap_locks",
    ]
    probes = {name: asyncio.Lock() for name in lock_attrs}
    for name in lock_attrs:
        getattr(coord, name)[TEST_CAM_ID] = probes[name]
        assert getattr(coord, name)[TEST_CAM_ID] is probes[name]

    coord._purge_cam_id(TEST_CAM_ID)

    still_present = [name for name in lock_attrs if TEST_CAM_ID in getattr(coord, name)]
    assert still_present == [], (
        f"_purge_cam_id left TEST_CAM_ID behind in Slice 4 lock fields: {still_present}"
    )


async def test_purge_cam_id_set_attr_loop_mechanism_still_works(
    hass: HomeAssistant,
) -> None:
    """`_PURGE_CAM_SET_ATTRS` became empty in Slice 3 (its last member,
    `user_intent_streams`, is now a `BoolFieldView` facade — see the
    excluded-list comment above `_PURGE_CAM_DICT_ATTRS` in `__init__.py`).
    The generic `for attr_name in self._PURGE_CAM_SET_ATTRS: ... .discard
    (cam_id)` loop body in `_purge_cam_id` is therefore currently
    unreached by any real attribute — this test exercises the loop
    mechanism directly against a synthetic set attribute (monkeypatched
    onto the tuple) so a future slice reintroducing a cam_id-keyed `set`
    attribute can rely on tested, not just theoretical, purge behavior.
    """
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord._fake_purge_set_attr = {TEST_CAM_ID, OTHER_CAM_ID}
    original = coord._PURGE_CAM_SET_ATTRS
    coord._PURGE_CAM_SET_ATTRS = (*original, "_fake_purge_set_attr")
    try:
        coord._purge_cam_id(TEST_CAM_ID)
    finally:
        coord._PURGE_CAM_SET_ATTRS = original

    assert TEST_CAM_ID not in coord._fake_purge_set_attr
    assert OTHER_CAM_ID in coord._fake_purge_set_attr


async def test_cleanup_stale_devices_purges_removed_camera(hass: HomeAssistant) -> None:
    """`cleanup_stale_devices` purges the per-cam caches for a camera that
    disappeared from the Bosch cloud account, not just the device registry."""
    from homeassistant.helpers import device_registry as dr

    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    # Seed caches for a camera that is about to "disappear".
    coord.rcp_dimmer_cache[TEST_CAM_ID] = 7
    coord.shc_state_cache[TEST_CAM_ID] = {"privacy_mode": False}
    coord.pan_cache[TEST_CAM_ID] = 3
    coord._sessions[TEST_CAM_ID] = coord.get_session(TEST_CAM_ID)

    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, TEST_CAM_ID)},
        name="Bosch Test Cam",
    )

    # Camera no longer present in the fresh cloud list.
    coord.cleanup_stale_devices(current_cam_ids=set())

    assert TEST_CAM_ID not in coord.rcp_dimmer_cache
    assert TEST_CAM_ID not in coord.shc_state_cache
    assert TEST_CAM_ID not in coord.pan_cache
    assert TEST_CAM_ID not in coord._sessions
    # Device entry itself is gone too (pre-existing behaviour, still true).
    assert dev_reg.async_get_device(identifiers={(DOMAIN, TEST_CAM_ID)}) is None


async def test_purge_cam_id_closes_leftover_tls_proxy_server(
    hass: HomeAssistant,
) -> None:
    """`tls_proxy_servers[cam_id]` must be popped synchronously (so it's
    already gone by the time `_purge_cam_id` returns, satisfying the
    completeness scan above) AND its `asyncio.Server` actually closed
    (`close()` + `close_clients()` + `wait_closed()`) via a tracked
    background task — dropping the reference alone would leak the
    listening socket for the rest of the HA process lifetime."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    mock_server = MagicMock()
    mock_server.close = MagicMock()
    mock_server.close_clients = MagicMock()
    mock_server.wait_closed = AsyncMock()
    coord.tls_proxy_servers[TEST_CAM_ID] = mock_server

    coord._purge_cam_id(TEST_CAM_ID)

    # Popped synchronously — no need to await anything to observe this.
    assert TEST_CAM_ID not in coord.tls_proxy_servers
    # The actual close I/O is deferred to a tracked background task.
    assert len(coord.bg_tasks) == 1
    await asyncio.gather(*coord.bg_tasks)

    mock_server.close.assert_called_once()
    mock_server.close_clients.assert_called_once()
    mock_server.wait_closed.assert_awaited_once()


async def test_purge_cam_id_tls_proxy_server_close_exception_is_swallowed(
    hass: HomeAssistant,
) -> None:
    """A raising close()/wait_closed() during the purge-triggered
    background close must be logged at DEBUG and swallowed, not crash the
    background task (which would otherwise surface as an unhandled
    exception in HA's log at an unrelated point in time)."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    mock_server = MagicMock()
    mock_server.close = MagicMock()
    mock_server.close_clients = MagicMock()
    mock_server.wait_closed = AsyncMock(side_effect=RuntimeError("synthetic"))
    coord.tls_proxy_servers[TEST_CAM_ID] = mock_server

    coord._purge_cam_id(TEST_CAM_ID)
    await asyncio.gather(*coord.bg_tasks)  # must not raise


async def test_purge_cam_id_stops_leftover_viewing_front_door(
    hass: HomeAssistant,
) -> None:
    """`viewing_sticky_port[cam_id]` presence must trigger an explicit
    `stop_viewing_front_door` call via a tracked background task — same
    "leftover listener would leak for the process lifetime" rationale as
    the `tls_proxy_servers` case above, since the actual listener lives
    inside the single shared `viewing_front_door_runner`, not in the plain
    int `viewing_sticky_port` dict the generic purge loop already pops."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.viewing_sticky_port[TEST_CAM_ID] = 55123
    coord.stop_viewing_front_door = AsyncMock()

    coord._purge_cam_id(TEST_CAM_ID)

    # Popped synchronously by the generic dict-attr loop.
    assert TEST_CAM_ID not in coord.viewing_sticky_port
    assert len(coord.bg_tasks) == 1
    await asyncio.gather(*coord.bg_tasks)
    coord.stop_viewing_front_door.assert_awaited_once_with(TEST_CAM_ID)


async def test_purge_cam_id_viewing_front_door_stop_exception_is_swallowed(
    hass: HomeAssistant,
) -> None:
    """A raising `stop_viewing_front_door` during the purge-triggered
    background stop must be logged at DEBUG and swallowed, not crash the
    background task."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.viewing_sticky_port[TEST_CAM_ID] = 55123
    coord.stop_viewing_front_door = AsyncMock(side_effect=RuntimeError("synthetic"))

    coord._purge_cam_id(TEST_CAM_ID)
    await asyncio.gather(*coord.bg_tasks)  # must not raise


async def test_purge_cam_id_no_viewing_front_door_bg_task_when_not_bound(
    hass: HomeAssistant,
) -> None:
    """No `viewing_sticky_port[cam_id]` entry → no background stop task is
    scheduled at all (nothing to stop)."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    coord.stop_viewing_front_door = AsyncMock()

    coord._purge_cam_id(TEST_CAM_ID)

    assert len(coord.bg_tasks) == 0
    coord.stop_viewing_front_door.assert_not_called()


async def test_purge_cam_id_viewing_front_door_stop_waits_for_stream_lock(
    hass: HomeAssistant,
) -> None:
    """Bug-hunt finding (TOCTOU race): unlike every other mutator of the
    viewing front-door state, this purge-triggered stop used to run WITHOUT
    the per-cam stream lock — a concurrent renewal holding that lock could
    resurrect a fresh listener + `viewing_sticky_port` entry for a camera
    that was just confirmed gone from the Bosch cloud account, orphaning a
    bound socket forever (nothing left to purge it again). The purge's
    background stop must now block until a concurrent lock-holder releases
    it, exactly like `tear_down_live_stream` already does for every other
    teardown caller."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.viewing_sticky_port[TEST_CAM_ID] = 55123
    coord.stop_viewing_front_door = AsyncMock()

    lock = coord.get_stream_lock(TEST_CAM_ID)
    await lock.acquire()  # simulate a concurrent renewal holding the lock

    coord._purge_cam_id(TEST_CAM_ID)
    await asyncio.sleep(0)  # let the bg task reach `async with lock` and block

    assert len(coord.bg_tasks) == 1
    bg_task = next(iter(coord.bg_tasks))
    assert not bg_task.done(), (
        "REGRESSION: purge-triggered viewing front-door stop must block on "
        "the stream lock while a concurrent renewal holds it, not race it."
    )
    coord.stop_viewing_front_door.assert_not_called()

    lock.release()  # renewal finished
    await asyncio.gather(bg_task)
    coord.stop_viewing_front_door.assert_awaited_once_with(TEST_CAM_ID)


async def test_purge_cam_id_viewing_front_door_repops_sticky_port_after_lock(
    hass: HomeAssistant,
) -> None:
    """If a concurrent renewal re-inserts `viewing_sticky_port[cam_id]`
    while the purge's background stop is waiting for the lock (it had
    already published a fresh listener before the purge's synchronous
    dict-pop ran), the purge must defensively re-pop it once it finally
    acquires the lock and stops the listener — a purged camera must not be
    left with a stale entry in this dict."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.viewing_sticky_port[TEST_CAM_ID] = 55123
    coord.stop_viewing_front_door = AsyncMock()

    lock = coord.get_stream_lock(TEST_CAM_ID)
    await lock.acquire()

    coord._purge_cam_id(TEST_CAM_ID)
    await asyncio.sleep(0)
    assert TEST_CAM_ID not in coord.viewing_sticky_port  # popped synchronously

    # Simulate the racing renewal re-inserting the entry while it still
    # holds the lock, then releasing.
    coord.viewing_sticky_port[TEST_CAM_ID] = 66234
    lock.release()

    bg_task = next(iter(coord.bg_tasks))
    await asyncio.gather(bg_task)

    coord.stop_viewing_front_door.assert_awaited_once_with(TEST_CAM_ID)
    assert TEST_CAM_ID not in coord.viewing_sticky_port, (
        "REGRESSION: a sticky-port entry re-inserted by a racing renewal "
        "must not survive a confirmed camera purge."
    )


# ─────────────────────────────────────────────────────────────────────────────
# REMOTE viewing front-door purge (remote_viewing_front_door.py) — mirrors
# every LOCAL viewing-front-door purge test above, since `_purge_cam_id`
# handles `remote_viewing_sticky_port`/`stop_remote_viewing_front_door`
# with the exact same lock-then-stop-then-defensive-repop logic.
# ─────────────────────────────────────────────────────────────────────────────


async def test_purge_cam_id_stops_leftover_remote_viewing_front_door(
    hass: HomeAssistant,
) -> None:
    """`remote_viewing_sticky_port[cam_id]` presence must trigger an
    explicit `stop_remote_viewing_front_door` call via a tracked background
    task — same rationale as the LOCAL case above, separate runner."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.remote_viewing_sticky_port[TEST_CAM_ID] = 55124
    coord.stop_remote_viewing_front_door = AsyncMock()

    coord._purge_cam_id(TEST_CAM_ID)

    assert TEST_CAM_ID not in coord.remote_viewing_sticky_port
    assert len(coord.bg_tasks) == 1
    await asyncio.gather(*coord.bg_tasks)
    coord.stop_remote_viewing_front_door.assert_awaited_once_with(TEST_CAM_ID)


async def test_purge_cam_id_remote_viewing_front_door_stop_exception_is_swallowed(
    hass: HomeAssistant,
) -> None:
    """A raising `stop_remote_viewing_front_door` during the purge-triggered
    background stop must be logged at DEBUG and swallowed, not crash the
    background task."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.remote_viewing_sticky_port[TEST_CAM_ID] = 55124
    coord.stop_remote_viewing_front_door = AsyncMock(
        side_effect=RuntimeError("synthetic")
    )

    coord._purge_cam_id(TEST_CAM_ID)
    await asyncio.gather(*coord.bg_tasks)  # must not raise


async def test_purge_cam_id_no_remote_viewing_front_door_bg_task_when_not_bound(
    hass: HomeAssistant,
) -> None:
    """No `remote_viewing_sticky_port[cam_id]` entry → no background stop
    task is scheduled at all (nothing to stop)."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)
    coord.stop_remote_viewing_front_door = AsyncMock()

    coord._purge_cam_id(TEST_CAM_ID)

    assert len(coord.bg_tasks) == 0
    coord.stop_remote_viewing_front_door.assert_not_called()


async def test_purge_cam_id_remote_viewing_front_door_stop_waits_for_stream_lock(
    hass: HomeAssistant,
) -> None:
    """Same TOCTOU-race protection as the LOCAL front-door purge: the
    background stop must block on the per-cam stream lock while a
    concurrent renewal holds it, not race it."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.remote_viewing_sticky_port[TEST_CAM_ID] = 55124
    coord.stop_remote_viewing_front_door = AsyncMock()

    lock = coord.get_stream_lock(TEST_CAM_ID)
    await lock.acquire()  # simulate a concurrent renewal holding the lock

    coord._purge_cam_id(TEST_CAM_ID)
    await asyncio.sleep(0)  # let the bg task reach `async with lock` and block

    assert len(coord.bg_tasks) == 1
    bg_task = next(iter(coord.bg_tasks))
    assert not bg_task.done(), (
        "REGRESSION: purge-triggered REMOTE viewing front-door stop must "
        "block on the stream lock while a concurrent renewal holds it, not "
        "race it."
    )
    coord.stop_remote_viewing_front_door.assert_not_called()

    lock.release()  # renewal finished
    await asyncio.gather(bg_task)
    coord.stop_remote_viewing_front_door.assert_awaited_once_with(TEST_CAM_ID)


async def test_purge_cam_id_remote_viewing_front_door_repops_sticky_port_after_lock(
    hass: HomeAssistant,
) -> None:
    """If a concurrent renewal re-inserts `remote_viewing_sticky_port[cam_id]`
    while the purge's background stop is waiting for the lock, the purge
    must defensively re-pop it once it finally acquires the lock and stops
    the listener."""
    entry = _make_entry(hass)
    coord = BoschCameraCoordinator(hass, entry)

    coord.remote_viewing_sticky_port[TEST_CAM_ID] = 55124
    coord.stop_remote_viewing_front_door = AsyncMock()

    lock = coord.get_stream_lock(TEST_CAM_ID)
    await lock.acquire()

    coord._purge_cam_id(TEST_CAM_ID)
    await asyncio.sleep(0)
    assert TEST_CAM_ID not in coord.remote_viewing_sticky_port  # popped synchronously

    coord.remote_viewing_sticky_port[TEST_CAM_ID] = 66235
    lock.release()

    bg_task = next(iter(coord.bg_tasks))
    await asyncio.gather(bg_task)

    coord.stop_remote_viewing_front_door.assert_awaited_once_with(TEST_CAM_ID)
    assert TEST_CAM_ID not in coord.remote_viewing_sticky_port, (
        "REGRESSION: a sticky-port entry re-inserted by a racing renewal "
        "must not survive a confirmed camera purge."
    )
