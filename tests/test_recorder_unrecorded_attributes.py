"""Regression pins for recorder DB-bloat hardening (HA#39).

Several diagnostic entities carry attributes that either change on every
coordinator/drain tick (freshness counters, rotating stream URLs) or hold
large card-only blobs (zone/mask coordinate lists). HA's recorder hashes
each state's attributes into the shared `state_attributes` table, so a
value that changes every tick (or a multi-KB list) bloats that table with
no history value.

`_unrecorded_attributes` strips the listed keys before the recorder stores
them — the attribute stays visible live, only its recording is suppressed.
These tests pin the exact excluded set per entity so a future edit that
adds a volatile/blob attribute without excluding it fails loudly.

Asserting on the class attribute keeps this fixture-free: `_unrecorded_attributes`
is a class-level frozenset, no coordinator stub required.
"""

from __future__ import annotations

import pytest

from custom_components.bosch_shc_camera.binary_sensor import (
    BoschLanReachableBinarySensor,
)
from custom_components.bosch_shc_camera.camera import BoschCamera
from custom_components.bosch_shc_camera.sensor import (
    BoschAlarmCatalogSensor,
    BoschCloudMaintenanceSensor,
    BoschFcmPushStatusSensor,
    BoschIvaCatalogSensor,
    BoschMotionZonesSensor,
    BoschNvrStateSensor,
    BoschPrivateAreasSensor,
    BoschRulesCountSensor,
)
from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

# (entity class, attribute keys that MUST be excluded from the recorder)
_CASES = [
    (BoschFcmPushStatusSensor, {"last_push_seconds_ago"}),
    (BoschCloudMaintenanceSensor, {"last_fetched_seconds_ago"}),
    (
        BoschLanReachableBinarySensor,
        {"last_check_seconds_ago", "write_grace_seconds_left"},
    ),
    (BoschRulesCountSensor, {"rules"}),
    (BoschAlarmCatalogSensor, {"alarm_details"}),
    (BoschMotionZonesSensor, {"zones", "coordinates", "cloud_zones", "gen2_zones"}),
    (BoschIvaCatalogSensor, {"modules", "active_modules"}),
    (BoschPrivateAreasSensor, {"cloud_privacy_masks", "gen2_private_areas"}),
    (
        BoschNvrStateSensor,
        {"last_segment_age_s", "last_tick_ts", "pending_uploads", "failed_uploads"},
    ),
    (BoschCamera, {"live_rtsps", "live_proxy", "stream_url"}),
    (BoschLiveStreamSwitch, {"rtsps_url", "proxy_snap_url"}),
]


@pytest.mark.parametrize(
    ("entity_cls", "expected"),
    _CASES,
    ids=[cls.__name__ for cls, _ in _CASES],
)
def test_volatile_and_blob_attrs_are_unrecorded(entity_cls, expected):
    """HA#39: every churning/blob attribute must be excluded from recording."""
    excluded = entity_cls._unrecorded_attributes
    missing = expected - set(excluded)
    assert not missing, (
        f"{entity_cls.__name__} must exclude {sorted(missing)} from the recorder "
        f"(state_attributes bloat). Current _unrecorded_attributes={sorted(excluded)}"
    )
