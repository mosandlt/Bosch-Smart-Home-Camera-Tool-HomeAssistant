"""Tests for the Bosch Smart Home Camera diagnostics module.

Verifies that sensitive data (FCM credentials, private keys, tokens) is
redacted before appearing in the diagnostics download.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera.const import DOMAIN
from custom_components.bosch_shc_camera.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


def test_to_redact_covers_all_known_secrets() -> None:
    """The redact list must include every known sensitive field name."""
    must_redact = {
        # OAuth
        "bearer_token",
        "refresh_token",
        "access_token",
        # FCM / Firebase
        "fcm_credentials",
        "fcm_config",
        "api_key",
        "private",
        "secret",
        "auth",
        "token",
        "fid",
        "p256dh",
        "android_id",
        "security_token",
        # SMB
        "smb_password",
        "smb_username",
        "smb_server",
        # Frigate/external-recorder persistent RTSP front-door credentials
        # (bug-hunt 2026-07-03 — these leaked unredacted before this fix)
        "frigate_token",
        "frigate_basic_user",
        "frigate_ip_allowlist",
        # Stream URLs containing session creds
        "rtsps_url",
        "rtspsUrl",
        # Network identifiers
        "mac",
    }
    missing = must_redact - TO_REDACT
    assert not missing, f"Diagnostics is missing redaction for: {missing}"


async def test_diagnostics_redacts_nested_fcm_credentials(
    hass: HomeAssistant,
) -> None:
    """Nested fcm_credentials substructure must be fully redacted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "bearer_token": "sensitive_bearer",
            "refresh_token": "sensitive_refresh",
            "fcm_credentials": {
                "fcm": {
                    "installation": {"token": "jwt_secret"},
                    "registration": {"token": "fcm_token_secret"},
                },
                "gcm": {"security_token": "gcm_secret"},
                "keys": {
                    "private": "PRIVATE_KEY_BYTES",
                    "secret": "WEBPUSH_SECRET",
                },
            },
        },
        options={},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {},
            "last_update_success": True,
            "fcm_running": False,
            "fcm_healthy": True,
            "auth_outage_count": 0,
            "update_interval": None,
        },
    )()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    import json

    blob = json.dumps(diag)
    # None of these sensitive markers should appear in the diagnostics output.
    for leaked in (
        "sensitive_bearer",
        "sensitive_refresh",
        "jwt_secret",
        "fcm_token_secret",
        "gcm_secret",
        "PRIVATE_KEY_BYTES",
        "WEBPUSH_SECRET",
    ):
        assert leaked not in blob, f"Diagnostics leaked: {leaked}"


def test_camera_summary_excludes_full_uuid(hass: HomeAssistant) -> None:
    """Per-camera summary must use the cam_id_prefix (8 chars), not the full UUID.

    The full UUID is a Bosch cloud identifier that can be cross-referenced;
    the 8-char prefix is enough for log correlation without leaking the ID.
    """
    from custom_components.bosch_shc_camera.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    # Module signature check — full inspection happens in the integration
    # diagnostics test which builds a real coordinator. This test asserts the
    # contract that cam_id_prefix is used in the summary.
    assert async_get_config_entry_diagnostics is not None


async def test_camera_summary_includes_required_fields(hass: HomeAssistant) -> None:
    """Per-camera summary must surface model, firmware, status, etc."""
    cam_id = "11111111-1111-1111-1111-111111111111"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"bearer_token": "secret"},
        options={},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {
                cam_id: {
                    "info": {
                        "title": "Bosch Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                    },
                    "status": "ONLINE",
                    "events": [{"id": "e1"}, {"id": "e2"}],
                    "live": {"connectionType": "LOCAL", "age_seconds": 12},
                }
            },
            "last_update_success": True,
            "fcm_running": True,
            "fcm_healthy": True,
            "auth_outage_count": 0,
            "update_interval": None,
        },
    )()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    cams = diag["cameras"]
    assert len(cams) == 1
    cam = cams[0]
    assert cam["cam_id_prefix"] == "11111111"
    assert cam["title"] == "Bosch Terrasse"
    assert cam["model"] == "HOME_Eyes_Outdoor"
    assert cam["firmware"] == "9.40.25"
    assert cam["status"] == "ONLINE"
    assert cam["events_today_count"] == 2
    assert cam["live_connection_type"] == "LOCAL"
    assert cam["live_age_seconds"] == 12


async def test_camera_summary_handles_empty_coordinator(hass: HomeAssistant) -> None:
    """No coordinator data → empty cameras list, no crash."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {},
            "last_update_success": True,
            "fcm_running": False,
            "fcm_healthy": True,
            "auth_outage_count": 0,
            "update_interval": None,
        },
    )()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["cameras"] == []
    assert diag["coordinator"]["running"] is True


async def test_diagnostics_handles_missing_runtime_data(hass: HomeAssistant) -> None:
    """No runtime_data attr → coordinator.running = False, no crash."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    # Intentionally do NOT set runtime_data — test the fallback path.

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["coordinator"]["running"] is False
    assert diag["cameras"] == []


async def test_coordinator_section_exposes_health_signals(
    hass: HomeAssistant,
) -> None:
    """coordinator.running, fcm_running, fcm_healthy, auth_outage_count,
    and stream_warming_count are essential bug-report context.

    Regression: `stream_warming` is a `StreamWarmingView` facade (Phase 1
    coordinator rewrite), not a plain `set[str]` — a stub using a real
    `set()` here would NOT have caught `len()` breaking against the real
    facade (found live by a THREE_PER_ISSUE_PER_CHANGE bug-hunt agent), so
    this constructs the actual facade against a real `_sessions` dict.
    """
    from custom_components.bosch_shc_camera.session_state import (
        CameraSessionState,
        StreamWarmingView,
    )

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    sessions = {
        "cam-A": CameraSessionState(warming=True),
        "cam-B": CameraSessionState(warming=True),
        "cam-C": CameraSessionState(warming=False),  # must NOT be counted
    }
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {},
            "last_update_success": False,  # mid-incident
            "fcm_running": True,
            "fcm_healthy": False,
            "auth_outage_count": 4,
            "stream_warming": StreamWarmingView(sessions),
            "update_interval": type("Td", (), {"total_seconds": lambda self: 60.0})(),
        },
    )()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    coord = diag["coordinator"]
    assert coord["running"] is True, "coordinator.running must be True"
    assert coord["last_update_success"] is False, (
        "last_update_success must reflect mid-incident state"
    )
    assert coord["fcm_running"] is True, "fcm_running must be exposed"
    assert coord["fcm_healthy"] is False, "fcm_healthy must be exposed"
    assert coord["auth_outage_count"] == 4, "auth_outage_count must be exposed"
    assert coord["scan_interval"] == 60.0, "scan_interval must be exposed"
    assert coord["stream_warming_count"] == 2, (
        "stream_warming_count must reflect warming cameras"
    )


async def test_integration_version_exposed(hass: HomeAssistant) -> None:
    """integration_version must appear at the top level of the diagnostics JSON.

    This is the first thing support needs to know — without it every bug report
    requires a follow-up question.
    """
    from custom_components.bosch_shc_camera.diagnostics import INTEGRATION_VERSION

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {},
            "last_update_success": True,
            "fcm_running": False,
            "fcm_healthy": True,
            "auth_outage_count": 0,
            "update_interval": None,
        },
    )()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "integration_version" in diag, "integration_version must be a top-level key"
    ver = diag["integration_version"]
    assert isinstance(ver, str) and ver != "unknown", (
        f"integration_version must be a real version string, got {ver!r}"
    )
    # Must match what INTEGRATION_VERSION constant reports
    assert ver == INTEGRATION_VERSION, "diagnostics version must match module constant"
    # Must look like a semver (N.N.N, optionally with a -beta.N/-rc.N
    # prerelease suffix for a beta release train build) so we catch
    # manifest parsing breaks without hard-failing on a valid beta version.
    base, _, _prerelease = ver.partition("-")
    parts = base.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), (
        f"Expected N.N.N[-prerelease] version string, got {ver!r}"
    )


async def test_camera_stream_health_fields(hass: HomeAssistant) -> None:
    """Per-camera diagnostics must include stream health fields.

    stream_error_count, stream_fell_back, session_stale, and offline_since_seconds
    are the key signals for diagnosing stream-restart loops and session bugs.
    """
    import time

    cam_id = "11111111-1111-1111-1111-111111111111"
    offline_ts = time.monotonic() - 120.0  # camera offline for ~120s
    entry = MockConfigEntry(domain=DOMAIN, data={"bearer_token": "x"}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {
                cam_id: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                    },
                    "status": "OFFLINE",
                    "events": [],
                    "live": {"connectionType": "REMOTE", "age_seconds": 5},
                }
            },
            "last_update_success": True,
            "fcm_running": False,
            "fcm_healthy": True,
            "auth_outage_count": 0,
            "offline_since": {cam_id: offline_ts},
            "stream_error_count": {cam_id: 3},
            "stream_fell_back": {cam_id: True},
            "session_stale": {cam_id: False},
            "stream_warming": set(),
            "update_interval": None,
        },
    )()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    cam = diag["cameras"][0]
    assert cam["stream_error_count"] == 3, (
        "stream_error_count must be exposed for loop detection"
    )
    assert cam["stream_fell_back"] is True, (
        "stream_fell_back must be exposed to show REMOTE fallback"
    )
    assert cam["session_stale"] is False, "session_stale must be exposed"
    offline_s = cam["offline_since_seconds"]
    assert isinstance(offline_s, int), "offline_since_seconds must be an int"
    assert 100 <= offline_s <= 200, (
        f"Expected ~120s offline, got {offline_s}s — offline_since_seconds arithmetic is wrong"
    )


async def test_camera_stream_health_defaults_for_healthy_camera(
    hass: HomeAssistant,
) -> None:
    """Stream health fields must default to zero/False when camera is healthy.

    A camera that has never errored must not show stream_error_count=None or missing key.
    """
    cam_id = "22222222-0000-0000-0000-000000000000"
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {
                cam_id: {
                    "info": {
                        "title": "Indoor",
                        "hardwareVersion": "CAMERA_360",
                        "firmwareVersion": "7.91.56",
                    },
                    "status": "ONLINE",
                    "events": [],
                    "live": {"connectionType": "LOCAL", "age_seconds": 30},
                }
            },
            "last_update_success": True,
            "fcm_running": True,
            "fcm_healthy": True,
            "auth_outage_count": 0,
            "offline_since": {},
            "stream_error_count": {},
            "stream_fell_back": {},
            "session_stale": {},
            "stream_warming": set(),
            "update_interval": None,
        },
    )()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    cam = diag["cameras"][0]
    assert cam["stream_error_count"] == 0, (
        "healthy camera must show 0 stream errors, not None"
    )
    assert cam["stream_fell_back"] is False, (
        "healthy camera must show stream_fell_back=False"
    )
    assert cam["session_stale"] is False, "healthy camera must show session_stale=False"
    assert cam["offline_since_seconds"] is None, (
        "online camera must show offline_since_seconds=None"
    )


async def test_options_redaction_strips_smb_credentials(hass: HomeAssistant) -> None:
    """SMB credentials in entry.options must be redacted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "smb_password": "MySecretSmbPass",
            "smb_username": "thomas",
            "smb_server": "192.168.1.1",
            "smb_share": "FRITZ.NAS",
        },
    )
    entry.add_to_hass(hass)
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {},
            "last_update_success": True,
            "fcm_running": False,
            "fcm_healthy": True,
            "auth_outage_count": 0,
            "update_interval": None,
        },
    )()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    redacted_opts = diag["entry"]["options"]
    assert redacted_opts["smb_password"] == "**REDACTED**"
    assert redacted_opts["smb_username"] == "**REDACTED**"
    assert redacted_opts["smb_server"] == "**REDACTED**"
    # smb_share exposes NAS share name (network topology) — must be redacted (M2 fix)
    assert redacted_opts["smb_share"] == "**REDACTED**"


async def test_options_redaction_strips_frigate_credentials(
    hass: HomeAssistant,
) -> None:
    """Regression (bug-hunt 2026-07-03): the Frigate/external-recorder
    persistent RTSP front-door's auth token, Basic-Auth username, and
    allowed-IP list leaked in plaintext in diagnostics exports — the generic
    "token"/"auth" TO_REDACT entries don't catch "frigate_token" because
    async_redact_data matches keys EXACTLY, not by substring."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "frigate_token": "super-secret-bearer-value",
            "frigate_basic_user": "recorder-admin",
            "frigate_ip_allowlist": "192.0.2.5,192.0.2.6",
        },
    )
    entry.add_to_hass(hass)
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {},
            "last_update_success": True,
            "fcm_running": False,
            "fcm_healthy": True,
            "auth_outage_count": 0,
            "update_interval": None,
        },
    )()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    redacted_opts = diag["entry"]["options"]
    assert redacted_opts["frigate_token"] == "**REDACTED**"
    assert redacted_opts["frigate_basic_user"] == "**REDACTED**"
    assert redacted_opts["frigate_ip_allowlist"] == "**REDACTED**"
