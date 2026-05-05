"""Regression tests for the privacy-mode cache race (PRIVACY_REVERT bug).

Bug discovered 2026-04-27: first OFF-toggle of the privacy switch visibly
reverts to ON for ~1-2 seconds, then settles. Second OFF-toggle works
immediately. Root cause: the SHC fetcher in shc.py overwrites the
`_shc_state_cache[cam_id]["privacy_mode"]` field on every poll without
honoring the `_privacy_set_at` write-lock that the cloud-fetcher path
already respects (__init__.py:1690).

Fixed 2026-05-05 by adding the same write-lock check inside
`async_update_shc_states`.

These tests pin the contract: when a user write happened within
`_WRITE_LOCK_SECS`, a stale SHC poll response must NOT overwrite
the freshly-set cache value.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _make_stub_coordinator(write_lock_secs: float = 30.0):
    """Minimal coordinator stub with the fields shc.py touches."""
    return SimpleNamespace(
        _shc_state_cache={},
        _privacy_set_at={},
        _light_set_at={},
        _shc_devices_raw=[{"id": "dev-1", "name": "terrasse"}],
        _last_shc_fetch=time.monotonic(),
        _WRITE_LOCK_SECS=write_lock_secs,
        # async_update_shc_states uses these for the SHC HTTP path:
        hass=SimpleNamespace(),
    )


async def _run_fetcher(
    coord,
    data,
    mock_response_value: str,
    *,
    light_value: str = "OFF",
):
    """Patch async_shc_request to return the given privacy + light state.

    SHC API response shape: {"state": {"value": "ENABLED"|"DISABLED"|"ON"|"OFF"}}.
    """
    from custom_components.bosch_shc_camera import shc

    async def _fake_request(_coord, method, path, *args, **kwargs):
        if path.endswith("/services/PrivacyMode"):
            return {"state": {"value": mock_response_value}}
        if path.endswith("/services/CameraLight"):
            return {"state": {"value": light_value}}
        return None

    with patch.object(shc, "shc_configured", return_value=True), \
         patch.object(shc, "async_shc_request", side_effect=_fake_request):
        await shc.async_update_shc_states(coord, data)


@pytest.mark.asyncio
async def test_user_off_toggle_survives_stale_shc_poll() -> None:
    """User toggles privacy OFF → SHC poll within lock window must not flip back.

    This is the regression guard for the PRIVACY_REVERT bug. Before the
    fix in shc.py, the fetcher at line 244 wrote `entry["privacy_mode"] = new_priv`
    unconditionally — overwriting the user's freshly-set OFF value with a
    stale ENABLED reading from the SHC.
    """
    coord = _make_stub_coordinator()
    cam_id = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
    # 1. User just toggled privacy OFF. Cloud setter writes the cache + lock.
    coord._shc_state_cache[cam_id] = {"device_id": "dev-1", "privacy_mode": False}
    coord._privacy_set_at[cam_id] = time.monotonic()  # fresh write
    # 2. SHC poll runs and sees stale ENABLED (cloud lag).
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="ENABLED")
    # 3. Cache must STILL show False — write-lock honored.
    assert coord._shc_state_cache[cam_id]["privacy_mode"] is False, (
        "PRIVACY_REVERT regression: SHC fetcher overwrote a fresh "
        "user-OFF write with a stale ENABLED reading. The write-lock "
        "in async_update_shc_states is broken."
    )


@pytest.mark.asyncio
async def test_shc_poll_applies_after_lock_expires() -> None:
    """Once `_WRITE_LOCK_SECS` has elapsed, SHC poll IS authoritative again."""
    coord = _make_stub_coordinator(write_lock_secs=5.0)
    cam_id = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
    coord._shc_state_cache[cam_id] = {"device_id": "dev-1", "privacy_mode": False}
    coord._privacy_set_at[cam_id] = time.monotonic() - 10.0  # lock expired 5s ago
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="ENABLED")
    assert coord._shc_state_cache[cam_id]["privacy_mode"] is True, (
        "After write-lock expires, SHC must be authoritative again — got stuck "
        "on cached value."
    )


@pytest.mark.asyncio
async def test_shc_poll_applies_when_no_recent_user_write() -> None:
    """No `_privacy_set_at` entry → no lock → SHC writes immediately."""
    coord = _make_stub_coordinator()
    cam_id = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
    # Fresh start: cache exists but no user-write timestamp recorded
    coord._shc_state_cache[cam_id] = {"device_id": "dev-1", "privacy_mode": None}
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="ENABLED")
    assert coord._shc_state_cache[cam_id]["privacy_mode"] is True


@pytest.mark.asyncio
async def test_shc_poll_no_overwrite_when_value_matches() -> None:
    """If SHC reports the same value the user wrote, no race — write goes through.

    Edge case: user writes OFF, then SHC also returns OFF. The fix should
    not over-protect — it should only block when there's a value MISMATCH.
    """
    coord = _make_stub_coordinator()
    cam_id = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
    coord._shc_state_cache[cam_id] = {"device_id": "dev-1", "privacy_mode": False}
    coord._privacy_set_at[cam_id] = time.monotonic()  # fresh
    data = {cam_id: {"info": {"title": "terrasse"}}}
    # SHC agrees: also OFF (DISABLED)
    await _run_fetcher(coord, data, mock_response_value="DISABLED")
    # Either branch (skip or apply) ends with False — both are correct.
    assert coord._shc_state_cache[cam_id]["privacy_mode"] is False


# ── camera_light cache race (same bug shape as privacy) ──────────────────


@pytest.mark.asyncio
async def test_user_light_off_survives_stale_shc_poll() -> None:
    """User toggles camera_light OFF → SHC poll within lock window must not flip back.

    Same bug shape as privacy_mode. Discovered + fixed 2026-05-05 by
    extending the write-lock check in async_update_shc_states.
    """
    coord = _make_stub_coordinator()
    cam_id = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
    # User just toggled light OFF
    coord._shc_state_cache[cam_id] = {"device_id": "dev-1", "camera_light": False}
    coord._light_set_at[cam_id] = time.monotonic()
    data = {cam_id: {"info": {"title": "terrasse"}}}
    # SHC poll sees stale ON
    await _run_fetcher(
        coord, data,
        mock_response_value="DISABLED",  # privacy stays OFF
        light_value="ON",                # but light still ON in cloud
    )
    assert coord._shc_state_cache[cam_id]["camera_light"] is False, (
        "camera_light cache race: SHC fetcher overwrote a fresh user-OFF "
        "with stale ON reading. Same bug shape as privacy_mode race."
    )


@pytest.mark.asyncio
async def test_light_shc_poll_applies_after_lock_expires() -> None:
    """Once `_WRITE_LOCK_SECS` has elapsed, SHC poll IS authoritative for light."""
    coord = _make_stub_coordinator(write_lock_secs=5.0)
    cam_id = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
    coord._shc_state_cache[cam_id] = {"device_id": "dev-1", "camera_light": False}
    coord._light_set_at[cam_id] = time.monotonic() - 10.0
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="DISABLED", light_value="ON")
    assert coord._shc_state_cache[cam_id]["camera_light"] is True


@pytest.mark.asyncio
async def test_light_shc_poll_when_no_recent_user_write() -> None:
    """No `_light_set_at` entry → no lock → SHC writes immediately."""
    coord = _make_stub_coordinator()
    cam_id = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
    coord._shc_state_cache[cam_id] = {"device_id": "dev-1", "camera_light": None}
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="DISABLED", light_value="ON")
    assert coord._shc_state_cache[cam_id]["camera_light"] is True
