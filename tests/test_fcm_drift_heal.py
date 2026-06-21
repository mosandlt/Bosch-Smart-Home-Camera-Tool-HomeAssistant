"""Regression tests for FCM deviceType-drift heal (Fix C++).

Bug (v12.4.5 live, 2026-05-18):
  Users who ran migration v2→v3 without Fix C had fcm_registered_token intact
  in entry.data. register_fcm_with_bosch's "token unchanged" fast-path fired,
  skipping re-registration. Bosch CBS kept the old deviceType=IOS registration
  while the HA client used the Android Firebase context. All FCM pushes were
  silently routed to the wrong sub-app, causing 3-4 minute delays (polling fallback).

  After force-switching Bosch state to deviceType=ANDROID via direct API POST:
  FCM push latency dropped from 3:43 min to 0.9 seconds. Root cause confirmed.

Fix (this file guards):
  register_fcm_with_bosch must skip POST ONLY when BOTH conditions hold:
    1. fcm_registered_token matches current _fcm_token
    2. fcm_registered_device_type == "ANDROID"
  If either condition is false → POST fires, marker gets written after success.

Cases:
  (a) Fresh install — no stored token, no stored marker → POST fires → both written
  (b) Already-healed — token matches AND marker == "ANDROID" → skip (fast-path)
  (c) Drift case (THE BUG) — token matches BUT marker missing/None/wrong → POST fires
  (d) Token-changed — different token regardless of marker → POST fires
  (e) Server returns 500 (sh:internal.error) → success (already registered), marker written
  (f) Server returns 401 → False, no marker written, no token written
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.fcm"


# ── shared helpers ────────────────────────────────────────────────────────────


def _make_coord(data: dict | None = None) -> SimpleNamespace:
    """Minimal coordinator stub for register_fcm_with_bosch tests."""
    entry_data: dict = data if data is not None else {}
    update_calls: list[dict] = []

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        update_calls.append(dict(kwargs))
        if "data" in kwargs:
            entry.data = kwargs["data"]  # type: ignore[assignment]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry),
    )
    entry = SimpleNamespace(data=entry_data)

    coord = SimpleNamespace(
        token="bearer-abc",
        _fcm_token="fcm-tok-new",
        _entry=entry,
        hass=hass,
    )
    # Attach update_calls so tests can inspect them
    coord._update_calls = update_calls  # type: ignore[attr-defined]
    return coord


def _make_mock_response(status: int, body: str = "") -> MagicMock:
    """Build an aiohttp-like async context manager response stub."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_session_post(resp_cm: MagicMock) -> MagicMock:
    """Return a mock session whose .post() returns resp_cm."""
    session = MagicMock()
    session.post = MagicMock(return_value=resp_cm)
    return session


# ── (a) Fresh install ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drift_heal_a_fresh_install_posts_and_writes_both_markers() -> None:
    """(a) Fresh install — no fcm_registered_token, no fcm_registered_device_type.

    register_fcm_with_bosch must:
    - NOT skip (no stored token → skip condition is false)
    - POST to /v11/devices with deviceType=ANDROID in the JSON body
    - Write BOTH fcm_registered_token AND fcm_registered_device_type="ANDROID"
      into entry.data after HTTP 204.

    Without writing fcm_registered_device_type, the drift-heal guard has no
    marker to check on the next restart and will POST every time (wasteful) or,
    if the guard is absent, skip and leave a stale IOS registration.
    """
    coord = _make_coord(data={})  # no stored token, no marker
    resp_cm = _make_mock_response(204)
    session = _make_session_post(resp_cm)

    posted_bodies: list[dict] = []

    original_post = session.post

    def _capturing_post(url: str, **kwargs: object) -> MagicMock:
        if "json" in kwargs:
            posted_bodies.append(dict(kwargs["json"]))  # type: ignore[arg-type]
        return original_post(url, **kwargs)

    session.post = _capturing_post

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True, "Fresh install must return True on HTTP 204"

    # POST body must declare ANDROID
    assert posted_bodies, "POST must have been called for a fresh install"
    assert posted_bodies[0].get("deviceType") == "ANDROID", (
        "POST body must contain deviceType=ANDROID — Bosch CBS routes pushes "
        "based on this field; IOS is now HTTP 500 for this OSS account."
    )
    assert posted_bodies[0].get("deviceToken") == "fcm-tok-new"

    # Both markers must be written
    assert coord._update_calls, "async_update_entry must have been called"
    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_token") == "fcm-tok-new", (
        "fcm_registered_token must be written to enable the skip-check on next restart."
    )
    assert written_data.get("fcm_registered_device_type") == "ANDROID", (
        "fcm_registered_device_type=ANDROID must be written so the drift-heal guard "
        "can verify the Bosch CBS registration is current on next restart."
    )


# ── (b) Already-healed — fast-path preserved ─────────────────────────────────


@pytest.mark.asyncio
async def test_drift_heal_b_already_healed_skips_post() -> None:
    """(b) token matches AND fcm_registered_device_type == "ANDROID" → skip.

    The fast-path must be preserved for the steady-state case: after a
    successful registration (drift healed or fresh install), every subsequent
    HA restart must skip the POST — as long as the registration is still FRESH
    (younger than FCM_REREGISTER_INTERVAL_SEC). Issue #36 added a periodic
    re-POST: a registration older than the interval (or one with no
    `fcm_registered_at` stamp) re-announces to heal a server-side-dropped Bosch
    registration. Here we stamp it now so the steady-state skip still holds.
    """
    import time

    coord = _make_coord(
        data={
            "fcm_registered_token": "fcm-tok-new",  # same as _fcm_token
            "fcm_registered_device_type": "ANDROID",
            "fcm_registered_at": time.time(),  # fresh → fast-path skip
        }
    )

    session = MagicMock()
    session.post = MagicMock()

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True, "Already-healed entry must return True without POST"
    (
        session.post.assert_not_called(),
        (
            "token unchanged + ANDROID marker present → POST must NOT fire. "
            "Firing it would cause unnecessary Bosch CBS round-trips on every restart."
        ),
    )
    assert not coord._update_calls, (
        "No async_update_entry call expected when fast-path skips."
    )


# ── (c) Drift case — THE BUG ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drift_heal_c_token_matches_but_marker_missing_forces_post() -> None:
    """(c) token matches BUT fcm_registered_device_type missing → POST fires.

    This is the exact state Thomas was in after running migration v2→v3
    without Fix C: fcm_registered_token == current token (migration left it
    intact), but no fcm_registered_device_type marker exists.

    The old skip-logic would see token==token and silently skip, leaving Bosch
    CBS with deviceType=IOS. FCM pushes would be silently routed to iOS sub-app,
    arriving 3-4 minutes late (polling fallback).

    The fix must detect this drift and force a POST regardless of token equality.
    """
    coord = _make_coord(
        data={
            "fcm_registered_token": "fcm-tok-new",  # token unchanged — old skip would fire
            # fcm_registered_device_type intentionally absent
        }
    )

    resp_cm = _make_mock_response(204)
    session = _make_session_post(resp_cm)

    posted_bodies: list[dict] = []
    original_post = session.post

    def _capturing_post(url: str, **kwargs: object) -> MagicMock:
        if "json" in kwargs:
            posted_bodies.append(dict(kwargs["json"]))  # type: ignore[arg-type]
        return original_post(url, **kwargs)

    session.post = _capturing_post

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True, "Drift-heal POST on HTTP 204 must return True"
    assert posted_bodies, (
        "POST must fire when fcm_registered_device_type is absent — "
        "without re-registration Bosch CBS keeps the stale deviceType=IOS entry."
    )
    assert posted_bodies[0].get("deviceType") == "ANDROID", (
        "Drift-heal POST body must contain deviceType=ANDROID."
    )

    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_device_type") == "ANDROID", (
        "After drift-heal POST, fcm_registered_device_type=ANDROID must be written "
        "so that the next restart uses the fast-path correctly."
    )
    assert written_data.get("fcm_registered_token") == "fcm-tok-new"


@pytest.mark.asyncio
async def test_drift_heal_c2_token_matches_but_marker_is_ios_forces_post() -> None:
    """(c2) token matches AND fcm_registered_device_type == "IOS" → POST fires.

    The marker could theoretically be IOS if written by old code. This variant
    ensures the drift-heal guard catches any non-ANDROID marker, not just None.
    """
    coord = _make_coord(
        data={
            "fcm_registered_token": "fcm-tok-new",
            "fcm_registered_device_type": "IOS",  # explicit wrong marker
        }
    )

    resp_cm = _make_mock_response(204)
    session = _make_session_post(resp_cm)

    posted_bodies: list[dict] = []
    original_post = session.post

    def _capturing_post(url: str, **kwargs: object) -> MagicMock:
        if "json" in kwargs:
            posted_bodies.append(dict(kwargs["json"]))  # type: ignore[arg-type]
        return original_post(url, **kwargs)

    session.post = _capturing_post

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True
    assert posted_bodies, "IOS marker must trigger drift-heal POST"
    assert posted_bodies[0].get("deviceType") == "ANDROID"
    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_device_type") == "ANDROID"


# ── (d) Token changed — existing behavior preserved ───────────────────────────


@pytest.mark.asyncio
async def test_drift_heal_d_token_changed_posts_and_writes_marker() -> None:
    """(d) different token, ANDROID marker present → POST fires.

    Token rotation must always trigger re-registration regardless of the
    deviceType marker. This is the existing stable behavior — ensure Fix C++
    does not accidentally gate on the marker when the token also changed.
    """
    coord = _make_coord(
        data={
            "fcm_registered_token": "fcm-tok-OLD",  # differs from _fcm_token
            "fcm_registered_device_type": "ANDROID",
        }
    )

    resp_cm = _make_mock_response(204)
    session = _make_session_post(resp_cm)

    posted_bodies: list[dict] = []
    original_post = session.post

    def _capturing_post(url: str, **kwargs: object) -> MagicMock:
        if "json" in kwargs:
            posted_bodies.append(dict(kwargs["json"]))  # type: ignore[arg-type]
        return original_post(url, **kwargs)

    session.post = _capturing_post

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True
    assert posted_bodies, "Token rotation must always trigger POST"
    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_token") == "fcm-tok-new"
    assert written_data.get("fcm_registered_device_type") == "ANDROID"


# ── (e) Server 500 sh:internal.error — already registered ─────────────────────


@pytest.mark.asyncio
async def test_drift_heal_e_server_500_internal_error_writes_marker() -> None:
    """(e) Server returns 500 sh:internal.error → treated as success, marker written.

    Bosch CBS returns HTTP 500 "sh:internal.error" when a token is already
    registered (duplicate). This is normal for the first restart after any
    registration. The integration treats it as success (FCM push will work)
    and must write BOTH markers to avoid repeating the POST on every restart.
    """
    coord = _make_coord(data={})  # no stored token

    resp_cm = _make_mock_response(
        500, body='{"code":"sh:internal.error","message":"Already exists"}'
    )
    session = _make_session_post(resp_cm)

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True, "HTTP 500 sh:internal.error must be treated as success"
    assert coord._update_calls, (
        "async_update_entry must have been called on 500 success path"
    )
    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_token") == "fcm-tok-new", (
        "fcm_registered_token must be written on 500 success path to avoid "
        "repeating the POST on every restart."
    )
    assert written_data.get("fcm_registered_device_type") == "ANDROID", (
        "fcm_registered_device_type=ANDROID must also be written on 500 success path."
    )


# ── (f) Server 401 — auth failure ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drift_heal_f_server_401_returns_false_no_marker_written() -> None:
    """(f) Server returns 401 → False, no marker written, no token written.

    Auth failure means the bearer token is invalid or expired. The function
    must return False and must NOT write any markers — writing them would
    cause the next restart to skip the POST even though registration failed,
    silently disabling FCM push.
    """
    coord = _make_coord(data={})  # no stored token

    resp_cm = _make_mock_response(
        401, body='{"code":"sh:no.permission","message":"Unauthorized"}'
    )
    session = _make_session_post(resp_cm)

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is False, "HTTP 401 must return False"
    assert not coord._update_calls, (
        "No async_update_entry call expected on auth failure — writing markers "
        "after a failed POST would cause the skip-check to fire on next restart "
        "and silently leave FCM push broken."
    )
