"""Tests for system_health.py — Bosch SHC Camera System Health dashboard support.

Covers every branch: no integration loaded, FCM healthy/degraded,
last_push=never vs. recent, and cloud-URL check coroutine passthrough.

Regression: ensures _fcm_last_push=float('-inf') returns "never" (not a
negative integer or crash) — CI VMs start fresh so monotonic() is small.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera.const import DOMAIN
from custom_components.bosch_shc_camera.system_health import (
    CLOUD_HEALTH_URL,
    _first_loaded_coordinator,
    _format_ago,
    async_register,
    system_health_info,
)

# ── Helper: build a minimal coordinator stub ─────────────────────────────────


def _make_coord(
    *,
    fcm_running: bool = True,
    fcm_last_push: float = float("-inf"),
    cameras: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal coordinator stub."""
    return type(
        "CoordStub",
        (),
        {
            "_fcm_running": fcm_running,
            "_fcm_last_push": fcm_last_push,
            "data": cameras if cameras is not None else {},
        },
    )()


# ── Unit tests for _format_ago ────────────────────────────────────────────────


def test_format_ago_never() -> None:
    """float('-inf') must return 'never', not crash or return a number."""
    assert _format_ago(float("-inf")) == "never"


def test_format_ago_recent() -> None:
    """A timestamp ~10s ago must return '10s ago' (±2s tolerance)."""
    ts = time.monotonic() - 10
    result = _format_ago(ts)
    assert result.endswith("s ago"), f"Expected 'Xs ago', got {result!r}"
    seconds = int(result.split("s")[0])
    assert 8 <= seconds <= 12, f"Expected ~10s, got {seconds}s"


def test_format_ago_zero_seconds() -> None:
    """A very recent push must return '0s ago' or '1s ago' (not crash)."""
    ts = time.monotonic()
    result = _format_ago(ts)
    assert result.endswith("s ago"), f"Expected 'Xs ago', got {result!r}"


def test_format_ago_positive_inf_returns_never_not_overflow() -> None:
    """float('+inf') must return 'never' rather than raising OverflowError.

    float('+inf') is not the intended sentinel (that's -inf), but it is a
    possible corrupt-state value. The function must handle it gracefully.
    """
    result = _format_ago(float("+inf"))
    # Both inf variants treated as "never" — no OverflowError
    assert result == "never"


# ── _first_loaded_coordinator ─────────────────────────────────────────────────


async def test_first_loaded_coordinator_no_entries(hass: HomeAssistant) -> None:
    """No config entries → returns None."""
    result = _first_loaded_coordinator(hass)
    assert result is None


async def test_first_loaded_coordinator_returns_runtime_data(
    hass: HomeAssistant,
) -> None:
    """Loaded entry → returns entry.runtime_data."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    coord = _make_coord()
    entry.runtime_data = coord

    # Simulate the entry being marked as loaded by patching async_loaded_entries
    with patch.object(
        hass.config_entries,
        "async_loaded_entries",
        return_value=[entry],
    ):
        result = _first_loaded_coordinator(hass)

    assert result is coord


async def test_first_loaded_coordinator_missing_runtime_data(
    hass: HomeAssistant,
) -> None:
    """Entry without runtime_data attribute → returns None (no AttributeError)."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    # Do NOT set entry.runtime_data

    with patch.object(
        hass.config_entries,
        "async_loaded_entries",
        return_value=[entry],
    ):
        result = _first_loaded_coordinator(hass)

    assert result is None


# ── async_register ────────────────────────────────────────────────────────────


def test_async_register_calls_register_info(hass: HomeAssistant) -> None:
    """async_register must call register.async_register_info with the right URL."""
    reg = MagicMock()
    async_register(hass, reg)
    reg.async_register_info.assert_called_once()
    args, kwargs = reg.async_register_info.call_args
    # First positional arg is the info callback, second (or kwarg) is manage_url
    assert args[0] is system_health_info
    manage_url = args[1] if len(args) > 1 else kwargs.get("manage_url")
    assert manage_url is not None
    assert "bosch_shc_camera" in manage_url


# ── system_health_info — no integration loaded ────────────────────────────────


async def test_system_health_no_integration(hass: HomeAssistant) -> None:
    """No entries loaded → defaults with cameras_loaded=0 and sentinel message."""
    fake_reach: Awaitable[str] = AsyncMock(return_value="ok")()

    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[]),
        patch(
            "custom_components.bosch_shc_camera.system_health.system_health.async_check_can_reach_url",
            return_value=fake_reach,
        ),
    ):
        info = await system_health_info(hass)

    assert info["cameras_loaded"] == 0
    assert info["fcm_push_active"] == "no integration loaded"
    assert "can_reach_cloud" in info
    assert "platinum_quality" in info
    # last_fcm_push_ago must NOT be present when coord is None
    assert "last_fcm_push_ago" not in info


# ── system_health_info — FCM healthy ─────────────────────────────────────────


async def test_system_health_fcm_healthy(hass: HomeAssistant) -> None:
    """FCM running → fcm_push_active='healthy', correct camera count."""
    coord = _make_coord(
        fcm_running=True,
        fcm_last_push=time.monotonic() - 30,
        cameras={"cam-A": {}, "cam-B": {}},
    )
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord

    fake_reach: Awaitable[str] = AsyncMock(return_value="ok")()

    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]),
        patch(
            "custom_components.bosch_shc_camera.system_health.system_health.async_check_can_reach_url",
            return_value=fake_reach,
        ),
    ):
        info = await system_health_info(hass)

    assert info["fcm_push_active"] == "healthy"
    assert info["cameras_loaded"] == 2
    assert info["last_fcm_push_ago"].endswith("s ago")
    assert info["platinum_quality"] == "v12.0.0+"


# ── system_health_info — FCM degraded ────────────────────────────────────────


async def test_system_health_fcm_degraded(hass: HomeAssistant) -> None:
    """FCM not running → fcm_push_active='degraded'."""
    coord = _make_coord(fcm_running=False, fcm_last_push=float("-inf"))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord

    fake_reach: Awaitable[str] = AsyncMock(return_value="ok")()

    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]),
        patch(
            "custom_components.bosch_shc_camera.system_health.system_health.async_check_can_reach_url",
            return_value=fake_reach,
        ),
    ):
        info = await system_health_info(hass)

    assert info["fcm_push_active"] == "degraded"


# ── system_health_info — last_fcm_push never ─────────────────────────────────


async def test_system_health_fcm_last_push_never(hass: HomeAssistant) -> None:
    """_fcm_last_push=float('-inf') → last_fcm_push_ago='never' (sentinel rule)."""
    coord = _make_coord(fcm_running=True, fcm_last_push=float("-inf"))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord

    fake_reach: Awaitable[str] = AsyncMock(return_value="ok")()

    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]),
        patch(
            "custom_components.bosch_shc_camera.system_health.system_health.async_check_can_reach_url",
            return_value=fake_reach,
        ),
    ):
        info = await system_health_info(hass)

    assert info["last_fcm_push_ago"] == "never", (
        f"Expected 'never' for float('-inf') last_push, got {info['last_fcm_push_ago']!r}"
    )


# ── system_health_info — last_fcm_push recent ────────────────────────────────


async def test_system_health_fcm_last_push_recent(hass: HomeAssistant) -> None:
    """_fcm_last_push ~5s ago → last_fcm_push_ago='5s ago' (±3s tolerance)."""
    coord = _make_coord(fcm_running=True, fcm_last_push=time.monotonic() - 5)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord

    fake_reach: Awaitable[str] = AsyncMock(return_value="ok")()

    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]),
        patch(
            "custom_components.bosch_shc_camera.system_health.system_health.async_check_can_reach_url",
            return_value=fake_reach,
        ),
    ):
        info = await system_health_info(hass)

    result = info["last_fcm_push_ago"]
    assert result.endswith("s ago"), f"Expected 'Xs ago', got {result!r}"
    seconds = int(result.split("s")[0])
    assert 2 <= seconds <= 8, f"Expected ~5s, got {seconds}s"


# ── system_health_info — cloud URL check is passed through as coroutine ───────


async def test_system_health_cloud_url_check_is_awaitable(
    hass: HomeAssistant,
) -> None:
    """async_check_can_reach_url result is included in the dict without being awaited.

    HA's system_health infrastructure awaits the value itself — we must NOT
    await it in system_health_info. We verify the function is called exactly
    once with the correct URL, and the return value ends up as can_reach_cloud.
    """
    coord = _make_coord(fcm_running=True, fcm_last_push=float("-inf"))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord

    # Use a plain MagicMock (not AsyncMock) so return_value is synchronous sentinel.
    sentinel = object()
    mock_check = MagicMock(return_value=sentinel)

    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]),
        patch(
            "custom_components.bosch_shc_camera.system_health.system_health.async_check_can_reach_url",
            mock_check,
        ),
    ):
        info = await system_health_info(hass)

    mock_check.assert_called_once_with(hass, CLOUD_HEALTH_URL)
    assert info["can_reach_cloud"] is sentinel, (
        "can_reach_cloud must be the raw return value of async_check_can_reach_url, "
        f"got {info['can_reach_cloud']!r}"
    )


# ── system_health_info — coordinator with no _fcm_last_push attr ──────────────


async def test_system_health_coord_missing_fcm_last_push(
    hass: HomeAssistant,
) -> None:
    """Coordinator without _fcm_last_push attr → falls back to float('-inf') → 'never'."""
    coord = type("CoordStub", (), {"_fcm_running": True, "data": {}})()
    # Note: _fcm_last_push is intentionally NOT set on this stub
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord

    fake_reach: Awaitable[str] = AsyncMock(return_value="ok")()

    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]),
        patch(
            "custom_components.bosch_shc_camera.system_health.system_health.async_check_can_reach_url",
            return_value=fake_reach,
        ),
    ):
        info = await system_health_info(hass)

    assert info["last_fcm_push_ago"] == "never"


# ── system_health_info — coordinator with no data attr ───────────────────────


async def test_system_health_coord_missing_data_attr(hass: HomeAssistant) -> None:
    """Coordinator without data attr → cameras_loaded=0, no crash."""
    coord = type(
        "CoordStub",
        (),
        {"_fcm_running": False, "_fcm_last_push": float("-inf")},
    )()
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord

    fake_reach: Awaitable[str] = AsyncMock(return_value="ok")()

    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]),
        patch(
            "custom_components.bosch_shc_camera.system_health.system_health.async_check_can_reach_url",
            return_value=fake_reach,
        ),
    ):
        info = await system_health_info(hass)

    assert info["cameras_loaded"] == 0


# ── CLOUD_HEALTH_URL sanity check ────────────────────────────────────────────


def test_cloud_health_url_points_to_bosch() -> None:
    """CLOUD_HEALTH_URL must point to the Bosch residential cloud endpoint."""
    assert "boschsecurity.com" in CLOUD_HEALTH_URL
    assert CLOUD_HEALTH_URL.startswith("https://")
