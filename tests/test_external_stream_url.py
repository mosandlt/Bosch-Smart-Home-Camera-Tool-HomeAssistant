"""Tests for the v12.4.0 substream-URL exposure feature.

Frigate / BlueIris users need RTSP URLs they can paste into external recorder
configs. Pre-v12.4 the URL was only on the camera entity's
`extra_state_attributes` and the inst=2 sub-stream was not exposed at all.

This release adds:
  - BoschExternalStreamSwitch (per camera, default OFF, RestoreEntity)
  - BoschStreamUrlSensor      (inst=1 main, value = None when switch OFF)
  - BoschStreamUrlSubSensor   (inst=2 sub, value = None when switch OFF)
  - _swap_inst() helper       (lone source of truth for the inst= rewrite)

These tests pin the contracts so a future refactor can't:
  - re-enable the switch by default (would spam every install with 2 sensors)
  - leak the URL through the sensor when the switch is OFF
  - forget the inst=N → inst=2 rewrite on the sub sensor
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"  # Terrasse Gen2 (Eyes Outdoor II)

LOCAL_RTSP_URL = (
    "rtsp://cbs-testuser:testpw@127.0.0.1:54321/rtsp_tunnel"
    "?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=3600"
)

# REMOTE path after the TLS proxy wraps the rtsps:// cloud URL — same shape,
# inst=4 because the REMOTE fallback uses a lower-bitrate stream by default.
REMOTE_RTSP_URL = (
    "rtsp://127.0.0.1:54322/rtsp_tunnel"
    "?inst=4&enableaudio=1&fmtp=1&maxSessionDuration=3600"
)


def _make_coord(rtsps_url: str | None = LOCAL_RTSP_URL) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "00:00:00:00:00:01",
                },
            }
        },
        _external_stream_enabled={},
        _live_connections=({CAM_ID: {"rtspsUrl": rtsps_url}} if rtsps_url else {}),
        last_update_success=True,
        async_update_listeners=MagicMock(),
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── _swap_inst helper ────────────────────────────────────────────────────────


def test_swap_inst_rewrites_inst_query_param() -> None:
    """The only place that knows the inst=N → inst=K substitution. Stay tiny."""
    from custom_components.bosch_shc_camera.sensor import _swap_inst

    assert _swap_inst(LOCAL_RTSP_URL, 2).endswith(
        "?inst=2&enableaudio=1&fmtp=1&maxSessionDuration=3600"
    )
    assert _swap_inst(REMOTE_RTSP_URL, 2).count("inst=2") == 1
    # Idempotent: inst=2 → inst=2 stays unchanged
    sub_url = _swap_inst(LOCAL_RTSP_URL, 2)
    assert _swap_inst(sub_url, 2) == sub_url


def test_swap_inst_only_touches_first_match() -> None:
    """Defensive: if a future bug puts inst=X in the path too, only the
    query-string match should be rewritten so the path stays canonical."""
    from custom_components.bosch_shc_camera.sensor import _swap_inst

    url = "rtsp://h:p@127.0.0.1:1/rtsp_tunnel?inst=1&foo=inst=99"
    out = _swap_inst(url, 2)
    # First inst= becomes 2; the literal "inst=99" inside the foo value
    # is left alone (it lives after the first &).
    assert out == "rtsp://h:p@127.0.0.1:1/rtsp_tunnel?inst=2&foo=inst=99"


# ── BoschExternalStreamSwitch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_default_off(stub_entry) -> None:
    """The switch ships disabled-by-default in the entity registry to keep
    the integration's first-run experience clean. Users opt in per camera."""
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_coord()
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    assert entity._attr_entity_registry_enabled_default is False
    assert entity.is_on is False


@pytest.mark.asyncio
async def test_switch_turn_on_sets_flag(stub_entry) -> None:
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_coord()
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert coord._external_stream_enabled[CAM_ID] is True
    assert entity.is_on is True
    # The two URL sensors recompute when the switch flips
    coord.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_switch_turn_off_clears_flag(stub_entry) -> None:
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_coord()
    coord._external_stream_enabled[CAM_ID] = True
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_off()

    assert coord._external_stream_enabled[CAM_ID] is False
    assert entity.is_on is False


@pytest.mark.asyncio
async def test_switch_restores_on_state_from_previous_session(stub_entry) -> None:
    """RestoreEntity: if the user had the switch ON before HA restart, the
    flag should come back without them having to re-toggle each cam."""
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_coord()
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    # Pretend HA restored a previous-session "on" state via the RestoreEntity
    # parent. The mixin reads it via async_get_last_state().
    last = SimpleNamespace(state="on")
    entity.async_get_last_state = AsyncMock(return_value=last)

    # Skip the real super().async_added_to_hass to keep the test focused on
    # the restore-logic only — the parent does HA-internal wiring.
    async def _noop(self: object) -> None:
        return None

    entity.__class__.__mro__[1].async_added_to_hass = _noop  # type: ignore[method-assign]

    await entity.async_added_to_hass()

    assert coord._external_stream_enabled[CAM_ID] is True


@pytest.mark.asyncio
async def test_switch_restore_off_does_not_set_flag(stub_entry) -> None:
    """Symmetric: a restored OFF state must NOT silently enable anything."""
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_coord()
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    entity.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="off"))

    async def _noop(self: object) -> None:
        return None

    entity.__class__.__mro__[1].async_added_to_hass = _noop  # type: ignore[method-assign]

    await entity.async_added_to_hass()

    assert coord._external_stream_enabled.get(CAM_ID, False) is False


# ── BoschStreamUrlSensor (main, inst=1) ──────────────────────────────────────


def test_main_sensor_returns_none_when_switch_off(stub_entry) -> None:
    """When the switch is OFF the sensor MUST NOT leak the URL — pin so a
    future refactor can't accidentally publish the raw URL on every install."""
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSensor

    coord = _make_coord()
    coord._external_stream_enabled[CAM_ID] = False
    sensor = BoschStreamUrlSensor(coord, CAM_ID, stub_entry)
    assert sensor.native_value is None


def test_main_sensor_returns_url_when_switch_on(stub_entry) -> None:
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSensor

    coord = _make_coord()
    coord._external_stream_enabled[CAM_ID] = True
    sensor = BoschStreamUrlSensor(coord, CAM_ID, stub_entry)
    assert sensor.native_value == LOCAL_RTSP_URL


def test_main_sensor_returns_none_when_no_session_open(stub_entry) -> None:
    """A switch flipped ON before any stream session exists must return None,
    not a partial/broken URL."""
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSensor

    coord = _make_coord(rtsps_url=None)
    coord._external_stream_enabled[CAM_ID] = True
    sensor = BoschStreamUrlSensor(coord, CAM_ID, stub_entry)
    assert sensor.native_value is None


# ── BoschStreamUrlSubSensor (sub, inst=2) ────────────────────────────────────


def test_sub_sensor_returns_none_when_switch_off(stub_entry) -> None:
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSubSensor

    coord = _make_coord()
    coord._external_stream_enabled[CAM_ID] = False
    sensor = BoschStreamUrlSubSensor(coord, CAM_ID, stub_entry)
    assert sensor.native_value is None


def test_sub_sensor_rewrites_inst_to_2(stub_entry) -> None:
    """Pin the value of the substream: same URL minus inst=N → inst=2."""
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSubSensor

    coord = _make_coord()
    coord._external_stream_enabled[CAM_ID] = True
    sensor = BoschStreamUrlSubSensor(coord, CAM_ID, stub_entry)
    val = sensor.native_value
    assert val is not None
    assert "inst=2" in val and "inst=1" not in val


def test_sub_sensor_rewrites_inst_4_to_2_on_remote(stub_entry) -> None:
    """REMOTE fallback uses inst=4 in the main URL; the sub-stream sensor
    still rewrites it to inst=2."""
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSubSensor

    coord = _make_coord(rtsps_url=REMOTE_RTSP_URL)
    coord._external_stream_enabled[CAM_ID] = True
    sensor = BoschStreamUrlSubSensor(coord, CAM_ID, stub_entry)
    val = sensor.native_value
    assert val is not None
    assert "inst=2" in val and "inst=4" not in val


def test_both_sensors_disabled_by_default(stub_entry) -> None:
    """Both URL sensors must be disabled in the entity registry by default —
    the switch is the one knob the user touches; the sensors come along for
    the ride. Stay disabled until the user picks them up via the UI."""
    from custom_components.bosch_shc_camera.sensor import (
        BoschStreamUrlSensor,
        BoschStreamUrlSubSensor,
    )

    coord = _make_coord()
    assert (
        BoschStreamUrlSensor(
            coord, CAM_ID, stub_entry
        )._attr_entity_registry_enabled_default
        is False
    )
    assert (
        BoschStreamUrlSubSensor(
            coord, CAM_ID, stub_entry
        )._attr_entity_registry_enabled_default
        is False
    )
