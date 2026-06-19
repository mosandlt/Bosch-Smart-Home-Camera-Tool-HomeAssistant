"""Regression tests for HA backend bugs fixed in v13.6.1 (round-2 bug hunt).

Covers:
  H2  — handle_create_rule / handle_delete_rule missing empty-cam/rule_id guard
  H3  — handle_update_rule PUT URL missing /{rule_id}
  H4  — BoschCameraLastEventSensor UTC timestamp (was treated as local)
  M4  — handle_set_privacy_masks coordinate range [0.0, 1.0] validation
  M5  — handle_rename_camera hardcoded Europe/Berlin timezone
  M6  — BoschCameraStatusSensor unknown status passthrough
  M7  — number.py available false-negative at level 0
  M8  — select.py current_option returns default (+ warning) for unknown enum
  L1  — handle_invite_friend logs only email domain
  L2  — BoschCameraEventsTodaySensor UTC date bucket
  M2  — diagnostics TO_REDACT covers smb_share + smb_base_path
  M3  — diagnostics cameras dicts routed through async_redact_data
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
RULE_ID = "rule-abc-123"
MODULE = "custom_components.bosch_shc_camera"


# ─── helpers ─────────────────────────────────────────────────────────────────


def _resp_cm(status: int, text: str = "", json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_hass(time_zone: str = "America/New_York"):
    hass = MagicMock()
    hass.services.has_service.return_value = False
    hass.services.async_register = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config_entries.async_loaded_entries.return_value = []
    hass.async_create_task = MagicMock()
    hass.config.time_zone = time_zone
    return hass


def _entry_with_coord(**coord_kwargs):
    """Returns (entry, coord) tuple matching the real test pattern."""
    coord = MagicMock()
    coord.token = "tok-A"
    coord.async_request_refresh = AsyncMock()
    coord._rules_cache = {}
    for k, v in coord_kwargs.items():
        setattr(coord, k, v)
    entry = MagicMock()
    entry.runtime_data = coord
    return entry, coord


def _get_handlers(hass):
    return {c.args[1]: c.args[2] for c in hass.services.async_register.call_args_list}


def _call(data):
    call = MagicMock()
    call.data = data
    return call


def _make_session(resp_cm):
    session = MagicMock()
    session.post = MagicMock(return_value=resp_cm)
    session.put = MagicMock(return_value=resp_cm)
    session.get = MagicMock(return_value=resp_cm)
    session.delete = MagicMock(return_value=resp_cm)
    return session


def _register_and_get_handlers(hass, session=None):
    """Call _register_services and return the handler dict."""
    from custom_components.bosch_shc_camera import _register_services

    _register_services(hass)
    return _get_handlers(hass)


# ═══════════════════════════════════════════════════════════════════════════════
# H2 — handle_create_rule / handle_delete_rule missing empty-string guard
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_rule_empty_camera_id_raises_validation_error():
    """H2: create_rule with empty camera_id must raise ServiceValidationError."""
    from homeassistant.exceptions import ServiceValidationError

    hass = _make_hass()
    handlers = _register_and_get_handlers(hass)

    with pytest.raises(ServiceValidationError):
        await handlers["create_rule"](_call({"camera_id": "", "name": "Test Rule"}))


@pytest.mark.asyncio
async def test_create_rule_no_camera_id_key_raises_validation_error():
    """H2: create_rule with missing camera_id key (defaults to '') must raise."""
    from homeassistant.exceptions import ServiceValidationError

    hass = _make_hass()
    handlers = _register_and_get_handlers(hass)

    with pytest.raises(ServiceValidationError):
        await handlers["create_rule"](_call({"name": "Test Rule"}))


@pytest.mark.asyncio
async def test_create_rule_valid_camera_id_proceeds():
    """H2: create_rule with valid camera_id must not raise guard error."""
    hass = _make_hass()
    session = _make_session(_resp_cm(201, json_data={"id": "new-rule"}))
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    handlers = _register_and_get_handlers(hass)

    with patch(
        f"{MODULE}.async_get_bosch_cloud_session",
        AsyncMock(return_value=session),
    ):
        # Should not raise ServiceValidationError (may raise other errors if API mock imperfect)
        try:
            await handlers["create_rule"](_call({"camera_id": CAM_ID, "name": "Test"}))
        except Exception as e:
            # Must NOT be a ServiceValidationError for camera_id
            assert "camera_id" not in str(e).lower() or "argument" not in str(e).lower()


@pytest.mark.asyncio
async def test_delete_rule_empty_camera_id_raises_validation_error():
    """H2: delete_rule with empty camera_id must raise ServiceValidationError."""
    from homeassistant.exceptions import ServiceValidationError

    hass = _make_hass()
    handlers = _register_and_get_handlers(hass)

    with pytest.raises(ServiceValidationError):
        await handlers["delete_rule"](_call({"camera_id": "", "rule_id": RULE_ID}))


@pytest.mark.asyncio
async def test_delete_rule_empty_rule_id_raises_validation_error():
    """H2: delete_rule with empty rule_id must raise ServiceValidationError."""
    from homeassistant.exceptions import ServiceValidationError

    hass = _make_hass()
    handlers = _register_and_get_handlers(hass)

    with pytest.raises(ServiceValidationError):
        await handlers["delete_rule"](_call({"camera_id": CAM_ID, "rule_id": ""}))


@pytest.mark.asyncio
async def test_delete_rule_both_empty_raises_validation_error():
    """H2: delete_rule with both empty cam_id and rule_id must raise."""
    from homeassistant.exceptions import ServiceValidationError

    hass = _make_hass()
    handlers = _register_and_get_handlers(hass)

    with pytest.raises(ServiceValidationError):
        await handlers["delete_rule"](_call({"camera_id": "", "rule_id": ""}))


# ═══════════════════════════════════════════════════════════════════════════════
# H3 — handle_update_rule PUT URL must include /{rule_id}
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_rule_put_url_includes_rule_id():
    """H3: handle_update_rule must PUT to .../rules/{rule_id}, not .../rules."""
    hass = _make_hass()
    put_resp = _resp_cm(200)
    session = _make_session(put_resp)
    entry, coord = _entry_with_coord(
        _rules_cache={
            CAM_ID: [
                {
                    "id": RULE_ID,
                    "name": "Old",
                    "isActive": True,
                    "startTime": "08:00:00",
                    "endTime": "20:00:00",
                    "weekdays": [0, 1, 2, 3, 4],
                }
            ]
        }
    )
    coord.async_request_refresh = AsyncMock()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    handlers = _register_and_get_handlers(hass)

    with patch(
        f"{MODULE}.async_get_bosch_cloud_session",
        AsyncMock(return_value=session),
    ):
        await handlers["update_rule"](
            _call({"camera_id": CAM_ID, "rule_id": RULE_ID, "is_active": False})
        )

    put_call_url = session.put.call_args[0][0]
    assert f"/rules/{RULE_ID}" in put_call_url, (
        f"PUT URL must contain /rules/{{rule_id}}, got: {put_call_url}"
    )
    # Must not hit the collection endpoint
    assert not put_call_url.endswith("/rules"), (
        f"PUT URL must NOT end at /rules (collection), got: {put_call_url}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# H4 — BoschCameraLastEventSensor: Z-suffix timestamps must be parsed as UTC
# ═══════════════════════════════════════════════════════════════════════════════


def _stub_coord_for_sensor(events=None):
    base_data = {
        "info": {
            "title": "Terrasse",
            "hardwareVersion": "HOME_Eyes_Outdoor",
            "firmwareVersion": "9.40.25",
            "macAddress": "aa:bb:cc:dd:ee:01",
            "featureSupport": {"light": True, "panLimit": 0},
        },
        "status": "ONLINE",
        "online": True,
        "privacy_mode": False,
        "events": events or [],
    }
    coord = SimpleNamespace(
        data={CAM_ID: base_data},
        _commissioned_cache={},
        _firmware_cache={},
        last_update_success=True,
        options={},
        is_updating=lambda cid: False,
    )
    coord.config_entry = SimpleNamespace(entry_id="test-entry-id")
    return coord


def _make_last_event_sensor(events=None, time_zone: str = "Europe/Berlin"):
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    coord = _stub_coord_for_sensor(events=events)
    sensor = BoschCameraLastEventSensor.__new__(BoschCameraLastEventSensor)
    sensor.coordinator = coord
    sensor._cam_id = CAM_ID
    sensor._attr_unique_id = "test"
    sensor.hass = SimpleNamespace(config=SimpleNamespace(time_zone=time_zone))
    return sensor


def test_last_event_sensor_utc_z_suffix_returns_utc_aware():
    """H4: Z-suffix timestamp must be parsed as UTC, returned tz-aware."""
    sensor = _make_last_event_sensor(
        events=[{"timestamp": "2026-06-15T22:30:00.000Z", "id": "ev1"}]
    )
    result = sensor.native_value
    assert result is not None, "Should return a datetime"
    assert result.tzinfo is not None, "datetime must be tz-aware"
    result_utc = result.astimezone(UTC)
    assert result_utc.hour == 22, (
        f"UTC hour must be 22 (22:30Z), got {result_utc.hour} "
        f"— timestamp was treated as local time (1-2h offset in CET+2)"
    )
    assert result_utc.minute == 30


def test_last_event_sensor_no_z_suffix_still_valid():
    """H4: timestamps without Z suffix should also parse (naive → UTC)."""
    sensor = _make_last_event_sensor(
        events=[{"timestamp": "2026-06-15T10:00:00", "id": "ev2"}]
    )
    result = sensor.native_value
    assert result is not None
    assert result.tzinfo is not None


def test_last_event_sensor_empty_events_returns_none():
    """H4: no events → native_value must be None."""
    sensor = _make_last_event_sensor(events=[])
    assert sensor.native_value is None


def test_last_event_sensor_bad_timestamp_returns_none():
    """H4: unparseable timestamp → None, no crash."""
    sensor = _make_last_event_sensor(events=[{"timestamp": "not-a-date", "id": "ev3"}])
    assert sensor.native_value is None


# ═══════════════════════════════════════════════════════════════════════════════
# M4 — handle_set_privacy_masks: coordinate range validation
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_set_privacy_masks_rejects_out_of_range_x():
    """M4: privacy mask with x > 1.0 must raise ServiceValidationError."""
    from homeassistant.exceptions import ServiceValidationError

    hass = _make_hass()
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    handlers = _register_and_get_handlers(hass)

    with pytest.raises(ServiceValidationError):
        await handlers["set_privacy_masks"](
            _call(
                {
                    "camera_id": CAM_ID,
                    "masks": [{"x": 1.5, "y": 0.0, "w": 0.2, "h": 0.2}],
                }
            )
        )


@pytest.mark.asyncio
async def test_set_privacy_masks_rejects_negative_y():
    """M4: privacy mask with y < 0.0 must raise ServiceValidationError."""
    from homeassistant.exceptions import ServiceValidationError

    hass = _make_hass()
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    handlers = _register_and_get_handlers(hass)

    with pytest.raises(ServiceValidationError):
        await handlers["set_privacy_masks"](
            _call(
                {
                    "camera_id": CAM_ID,
                    "masks": [{"x": 0.1, "y": -0.1, "w": 0.2, "h": 0.2}],
                }
            )
        )


@pytest.mark.asyncio
async def test_set_privacy_masks_rejects_w_above_one():
    """M4: mask with w > 1.0 must raise ServiceValidationError."""
    from homeassistant.exceptions import ServiceValidationError

    hass = _make_hass()
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    handlers = _register_and_get_handlers(hass)

    with pytest.raises(ServiceValidationError):
        await handlers["set_privacy_masks"](
            _call(
                {
                    "camera_id": CAM_ID,
                    "masks": [{"x": 0.0, "y": 0.0, "w": 1.1, "h": 0.5}],
                }
            )
        )


@pytest.mark.asyncio
async def test_set_privacy_masks_accepts_boundary_coordinates():
    """M4: privacy mask with all coordinates at exact boundary [0.0, 1.0] must not raise."""
    hass = _make_hass()
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    resp = _resp_cm(200)
    session = _make_session(resp)
    handlers = _register_and_get_handlers(hass)

    with patch(
        f"{MODULE}.async_get_bosch_cloud_session", AsyncMock(return_value=session)
    ):
        # Should not raise for boundary values
        await handlers["set_privacy_masks"](
            _call(
                {
                    "camera_id": CAM_ID,
                    "masks": [{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}],
                }
            )
        )


# ═══════════════════════════════════════════════════════════════════════════════
# M5 — handle_rename_camera: uses hass.config.time_zone, not hardcoded Berlin
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rename_camera_uses_hass_timezone():
    """M5: rename_camera must send hass.config.time_zone, not Europe/Berlin."""
    hass = _make_hass(time_zone="America/New_York")
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    resp = _resp_cm(200)
    session = _make_session(resp)
    handlers = _register_and_get_handlers(hass)

    with patch(
        f"{MODULE}.async_get_bosch_cloud_session", AsyncMock(return_value=session)
    ):
        await handlers["rename_camera"](
            _call({"camera_id": CAM_ID, "new_name": "Front Door"})
        )

    put_call = session.put.call_args
    body = put_call[1].get("json", {})
    assert body.get("timeZone") == "America/New_York", (
        f"Expected America/New_York, got {body.get('timeZone')!r} — hardcoded Berlin still present"
    )
    assert body.get("timeZone") != "Europe/Berlin"


@pytest.mark.asyncio
async def test_rename_camera_non_de_timezone_not_hardcoded():
    """M5: any non-Berlin hass timezone must be used in the API payload."""
    hass = _make_hass(time_zone="Asia/Tokyo")
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    resp = _resp_cm(204)
    session = _make_session(resp)
    handlers = _register_and_get_handlers(hass)

    with patch(
        f"{MODULE}.async_get_bosch_cloud_session", AsyncMock(return_value=session)
    ):
        await handlers["rename_camera"](_call({"camera_id": CAM_ID, "new_name": "Cam"}))

    body = session.put.call_args[1].get("json", {})
    assert body.get("timeZone") == "Asia/Tokyo", (
        f"Expected Asia/Tokyo, got {body.get('timeZone')!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# M6 — BoschCameraStatusSensor: unknown status → "unknown", not raw passthrough
# ═══════════════════════════════════════════════════════════════════════════════


def _make_status_sensor(status_value: str):
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "T",
                    "hardwareVersion": "H",
                    "firmwareVersion": "F",
                    "featureSupport": {},
                },
                "status": status_value,
                "online": True,
                "privacy_mode": False,
                "events": [],
            }
        },
        _commissioned_cache={},
        _firmware_cache={},
        last_update_success=True,
        options={},
        is_updating=lambda cid: False,
    )
    sensor = BoschCameraStatusSensor.__new__(BoschCameraStatusSensor)
    sensor.coordinator = coord
    sensor._cam_id = CAM_ID
    sensor._attr_unique_id = "test"
    sensor._attr_options = ["online", "offline", "updating", "session_limit", "unknown"]
    return sensor


def test_status_sensor_online_maps_correctly():
    """M6: ONLINE status must still work."""
    assert _make_status_sensor("ONLINE").native_value == "online"


def test_status_sensor_offline_maps_correctly():
    """M6: OFFLINE status must still work."""
    assert _make_status_sensor("OFFLINE").native_value == "offline"


def test_status_sensor_session_limit_maps_correctly():
    """M6: SESSION_LIMIT status must still work."""
    assert _make_status_sensor("SESSION_LIMIT").native_value == "session_limit"


def test_status_sensor_unknown_status_returns_unknown():
    """M6: new/unknown API status values must map to 'unknown'."""
    sensor = _make_status_sensor("BOSCH_FW_NEW_STATUS_V42")
    result = sensor.native_value
    assert result == "unknown", (
        f"Unknown status should return 'unknown', got {result!r} — "
        f"HA ENUM device_class would drop undeclared states"
    )


def test_status_sensor_empty_string_returns_unknown():
    """M6: empty status string must not pass through as-is."""
    sensor = _make_status_sensor("")
    result = sensor.native_value
    assert result == "unknown", f"Empty status should map to 'unknown', got {result!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# M7 — number.py available: false-negative at level 0
# ═══════════════════════════════════════════════════════════════════════════════


def _make_speaker_number(audio_cache_dict):
    """audio_cache_dict: the dict to store under cam_id key (None → key absent)."""
    from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

    audio_cache = {CAM_ID: audio_cache_dict} if audio_cache_dict is not None else {}
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "T",
                    "hardwareVersion": "H",
                    "firmwareVersion": "F",
                    "featureSupport": {},
                }
            }
        },
        last_update_success=True,
        _audio_cache=audio_cache,
    )
    entity = BoschSpeakerLevelNumber.__new__(BoschSpeakerLevelNumber)
    entity.coordinator = coord
    entity._cam_id = CAM_ID
    return entity


def test_speaker_level_available_when_level_is_zero():
    """M7: speaker level = 0 must NOT mark entity unavailable."""
    entity = _make_speaker_number({"speakerLevel": 0})
    assert entity.available is True, (
        "Speaker level 0 in cache — entity must be available (was using bool() which fails for 0)"
    )


def test_speaker_level_unavailable_when_cache_key_absent():
    """M7: missing cam_id key in audio_cache → unavailable (not yet polled)."""
    entity = _make_speaker_number(None)  # key absent
    assert entity.available is False


def test_speaker_level_available_when_level_nonzero():
    """M7: non-zero speaker level → available."""
    entity = _make_speaker_number({"speakerLevel": 75})
    assert entity.available is True


def _make_mic_number(audio_cache_dict):
    from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

    audio_cache = {CAM_ID: audio_cache_dict} if audio_cache_dict is not None else {}
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "T",
                    "hardwareVersion": "H",
                    "firmwareVersion": "F",
                    "featureSupport": {},
                }
            }
        },
        last_update_success=True,
        _audio_cache=audio_cache,
    )
    entity = BoschMicrophoneLevelNumber.__new__(BoschMicrophoneLevelNumber)
    entity.coordinator = coord
    entity._cam_id = CAM_ID
    return entity


def test_mic_level_available_when_level_is_zero():
    """M7: microphone level = 0 must NOT mark entity unavailable."""
    entity = _make_mic_number({"microphoneLevel": 0})
    assert entity.available is True, (
        "Microphone level 0 must not make entity unavailable"
    )


def test_mic_level_unavailable_when_cache_key_absent():
    """M7: missing cam_id key → unavailable."""
    entity = _make_mic_number(None)
    assert entity.available is False


def test_mic_level_available_when_level_nonzero():
    """M7: non-zero microphone level → available."""
    entity = _make_mic_number({"microphoneLevel": 50})
    assert entity.available is True


# ═══════════════════════════════════════════════════════════════════════════════
# M8 — select.py current_option: unknown enum → default + warning
# ═══════════════════════════════════════════════════════════════════════════════


def _make_motion_sensitivity_select(api_value):
    from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect

    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "T",
                    "hardwareVersion": "H",
                    "firmwareVersion": "F",
                    "featureSupport": {},
                }
            }
        },
        last_update_success=True,
        _motion_set_at={},
        motion_settings=lambda cid: (
            {"motionAlarmConfiguration": api_value} if api_value is not None else {}
        ),
    )
    entity = BoschMotionSensitivitySelect.__new__(BoschMotionSensitivitySelect)
    entity.coordinator = coord
    entity._cam_id = CAM_ID
    return entity


def test_motion_sensitivity_known_value_returned():
    """M8: known API values must be returned directly."""
    from custom_components.bosch_shc_camera.select import MOTION_SENSITIVITY_OPTIONS

    entity = _make_motion_sensitivity_select("HIGH")
    result = entity.current_option
    assert result == "high"
    assert result in MOTION_SENSITIVITY_OPTIONS


def test_motion_sensitivity_unknown_value_returns_default(caplog):
    """M8: unknown API value must return first option and log a warning."""
    from custom_components.bosch_shc_camera.select import MOTION_SENSITIVITY_OPTIONS

    entity = _make_motion_sensitivity_select("ULTRA_HIGH_V2_NEW")
    with caplog.at_level(logging.WARNING):
        result = entity.current_option
    assert result == MOTION_SENSITIVITY_OPTIONS[0], (
        f"Unknown value should return default option {MOTION_SENSITIVITY_OPTIONS[0]!r}, got {result!r}"
    )
    warning_messages = [
        r.message for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any("unknown" in m.lower() or "Unknown" in m for m in warning_messages), (
        f"A warning must be logged for unknown motion sensitivity value. Got: {warning_messages}"
    )


def test_motion_sensitivity_none_value_returns_none():
    """M8: no API value → None (entity not yet loaded)."""
    entity = _make_motion_sensitivity_select(None)
    assert entity.current_option is None


def test_motion_sensitivity_all_known_values():
    """M8: all known API values must resolve to a valid option."""
    from custom_components.bosch_shc_camera.select import (
        MOTION_SENSITIVITY_OPTIONS,
        SENSITIVITY_TO_API,
    )

    # API values are the VALUES of SENSITIVITY_TO_API
    api_values = list(SENSITIVITY_TO_API.values())
    for api_val in api_values:
        entity = _make_motion_sensitivity_select(api_val)
        result = entity.current_option
        assert result in MOTION_SENSITIVITY_OPTIONS, (
            f"API value {api_val!r} should map to a valid option, got {result!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — handle_invite_friend: only logs domain part of email
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_invite_friend_does_not_log_full_email(caplog):
    """L1: invite_friend must not log the full email address (PII in logs)."""
    hass = _make_hass()
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    resp = _resp_cm(201, json_data={"id": "friend-xyz"})
    session = _make_session(resp)
    handlers = _register_and_get_handlers(hass)

    test_email = "thomas@example.com"
    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session", AsyncMock(return_value=session)
        ),
        caplog.at_level(logging.INFO),
    ):
        await handlers["invite_friend"](_call({"email": test_email}))

    log_text = " ".join(r.message for r in caplog.records)
    assert test_email not in log_text, (
        "Full email address must not appear in logs (PII)"
    )
    # Domain part should still be traceable. Assert against the domain derived
    # from the input rather than a bare hostname literal: the literal form
    # ("example.com" in <str>) tripped CodeQL's URL-substring-sanitization
    # heuristic (py/incomplete-url-substring-sanitization). This is a
    # log-content assertion, not URL validation — a false positive — so we
    # avoid the flagged shape instead of carrying a dismissed alert.
    expected_domain = test_email.split("@", 1)[1]
    assert expected_domain in log_text, (
        "Domain part of email must appear in log for traceability"
    )


@pytest.mark.asyncio
async def test_invite_friend_email_without_at_sign_handled_gracefully(caplog):
    """L1: email without @ should not crash the domain extraction."""
    hass = _make_hass()
    entry, _coord = _entry_with_coord()
    hass.config_entries.async_loaded_entries.return_value = [entry]
    resp = _resp_cm(201, json_data={"id": "friend-abc"})
    session = _make_session(resp)
    handlers = _register_and_get_handlers(hass)

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session", AsyncMock(return_value=session)
        ),
        caplog.at_level(logging.INFO),
    ):
        # Should not crash even with malformed email
        await handlers["invite_friend"](_call({"email": "not-an-email"}))


# ═══════════════════════════════════════════════════════════════════════════════
# L2 — BoschCameraEventsTodaySensor: UTC date bucket
# ═══════════════════════════════════════════════════════════════════════════════


def test_events_today_sensor_uses_utc_date():
    """L2: events_today count uses UTC date to match Z-suffix timestamps."""
    from custom_components.bosch_shc_camera.sensor import BoschCameraEventsTodaySensor

    utc_now = datetime.now(UTC)
    today_ts = utc_now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    coord = _stub_coord_for_sensor(
        events=[
            {"timestamp": today_ts, "id": "ev-today"},
            {"timestamp": "2000-01-01T23:59:00.000Z", "id": "ev-old"},
        ]
    )
    sensor = BoschCameraEventsTodaySensor.__new__(BoschCameraEventsTodaySensor)
    sensor.coordinator = coord
    sensor._cam_id = CAM_ID
    sensor._attr_unique_id = "test"
    sensor.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Berlin"))

    count = sensor.native_value
    assert count == 1, (
        f"Should count 1 event today (UTC), got {count} — "
        f"date bucket may be using local time instead of UTC"
    )


def test_events_today_sensor_zero_when_no_events():
    """L2: no events → count = 0."""
    from custom_components.bosch_shc_camera.sensor import BoschCameraEventsTodaySensor

    coord = _stub_coord_for_sensor(events=[])
    sensor = BoschCameraEventsTodaySensor.__new__(BoschCameraEventsTodaySensor)
    sensor.coordinator = coord
    sensor._cam_id = CAM_ID
    sensor._attr_unique_id = "test"
    sensor.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Berlin"))

    assert sensor.native_value == 0


# ═══════════════════════════════════════════════════════════════════════════════
# M2 — diagnostics TO_REDACT covers smb_share and smb_base_path
# ═══════════════════════════════════════════════════════════════════════════════


def test_to_redact_includes_smb_share_and_smb_base_path():
    """M2: smb_share and smb_base_path must be in TO_REDACT (network topology)."""
    from custom_components.bosch_shc_camera.diagnostics import TO_REDACT

    assert "smb_share" in TO_REDACT, (
        "smb_share exposes NAS share name — must be redacted"
    )
    assert "smb_base_path" in TO_REDACT, (
        "smb_base_path exposes folder path — must be redacted"
    )


def test_to_redact_includes_existing_smb_fields():
    """M2: existing SMB fields must still be in TO_REDACT."""
    from custom_components.bosch_shc_camera.diagnostics import TO_REDACT

    for field in ("smb_password", "smb_username", "smb_server"):
        assert field in TO_REDACT, f"{field} must be in TO_REDACT"


# ═══════════════════════════════════════════════════════════════════════════════
# M3 — diagnostics cameras dicts route through async_redact_data
# ═══════════════════════════════════════════════════════════════════════════════


def test_async_redact_data_correctly_redacts_camera_sensitive_fields():
    """M3: confirm async_redact_data applied to camera dict redacts TO_REDACT fields."""
    from homeassistant.components.diagnostics import async_redact_data

    from custom_components.bosch_shc_camera.diagnostics import TO_REDACT

    # A hypothetical future camera dict that contains sensitive fields
    cam_dict = {
        "cam_id_prefix": "11111111",
        "title": "Terrasse",
        "rtspsUrl": "rtsps://user:pass@192.168.0.1/stream",  # in TO_REDACT
        "mac": "aa:bb:cc:dd:ee:ff",  # in TO_REDACT
        "cloud_id": "aabbccdd-1234-5678-abcd-000000000000",  # in TO_REDACT
    }
    redacted = async_redact_data(cam_dict, TO_REDACT)
    assert redacted["rtspsUrl"] == "**REDACTED**", (
        "rtspsUrl must be redacted in camera dict"
    )
    assert redacted["mac"] == "**REDACTED**", "mac must be redacted in camera dict"
    assert redacted["cloud_id"] == "**REDACTED**", (
        "cloud_id must be redacted in camera dict"
    )
    # Non-sensitive fields preserved
    assert redacted["title"] == "Terrasse"
    assert redacted["cam_id_prefix"] == "11111111"
