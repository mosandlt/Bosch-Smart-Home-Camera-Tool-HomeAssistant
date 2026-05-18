"""PIN_EVERY_MODE tests for the simplified FCM push-mode select (v12.4.5).

Context:
  fcm_push_mode was simplified from 4 options (auto/android/ios/polling) to 2
  (auto/polling). The OSS-sanctioned Android Firebase key handles both platforms
  transparently; iOS-specific code path removed. Legacy values coerce to 'auto'
  via migration v2→v3.

PIN_EVERY_MODE rule: one explicit test per mode + one for the default + one for
garbage input. Never collapse to a single default-test that silently passes when
defaults change.

Migration tests (v2→v3) pin the async_migrate_entry rewrites. Pattern copied
from tests/test_local_first_default.py (v1→v2 migration tests).
"""
from __future__ import annotations

from types import SimpleNamespace
from threading import RLock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


MODULE = "custom_components.bosch_shc_camera.fcm"


# ── shared helpers ─────────────────────────────────────────────────────────────

def _make_coord(**overrides: object) -> SimpleNamespace:
    """Minimal coordinator stub for FCM mode tests."""
    base = dict(
        token="tok-A",
        _fcm_token="fcm-token-xyz",
        _fcm_push_mode="unknown",
        _fcm_lock=RLock(),
        _fcm_running=False,
        _fcm_healthy=False,
        _fcm_client=None,
        _entry=SimpleNamespace(data={}),
        options={"enable_fcm_push": True, "fcm_push_mode": "auto"},
        hass=MagicMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_migrate_harness(version: int, options: dict) -> tuple:
    """Return (hass, entry, captured) for async_migrate_entry tests."""
    captured: dict = {}

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        captured.update(kwargs)
        if "options" in kwargs:
            entry.options = kwargs["options"]
        if "version" in kwargs:
            entry.version = kwargs["version"]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(entry_id="test-entry", version=version, options=options)
    return hass, entry, captured


# ── PIN_EVERY_MODE: auto ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_mode_calls_fcm_register() -> None:
    """PIN auto: fcm_push_mode='auto' → register_fcm_with_bosch is invoked once.

    The OSS-key FCM path (_try_fcm) must attempt a Bosch device registration
    whenever the user selects 'auto'. Failure to call register means pushes
    never arrive and motion events are silently delayed to polling interval.

    FcmRegisterConfig is imported lazily inside async_start_fcm_push, so we
    patch it via firebase_messaging module-level and patch the lazy import.
    """
    register_called: list[bool] = []

    async def _fake_register(c: object) -> bool:
        register_called.append(True)
        return True

    coord = _make_coord(options={"enable_fcm_push": True, "fcm_push_mode": "auto"})

    mock_client = MagicMock()
    mock_client.checkin_or_register = AsyncMock(return_value="fcm-tok")
    mock_client.start = AsyncMock()
    mock_client_cls = MagicMock(return_value=mock_client)

    # FcmRegisterConfig is imported inside the function: patch the module attribute
    # on the firebase_messaging stub so the lazy `from firebase_messaging import` works.
    fake_firebase = MagicMock()
    fake_firebase.FcmRegisterConfig = MagicMock()
    fake_firebase.FcmPushClientConfig = None  # optional import guarded with try/except

    with (
        patch(f"{MODULE}.register_fcm_with_bosch", side_effect=_fake_register),
        patch(f"{MODULE}._install_fcm_noise_filter"),
        patch(f"{MODULE}._get_fcm_push_client_class", return_value=mock_client_cls),
        patch(f"{MODULE}.fetch_firebase_config", new=AsyncMock(return_value={
            "api_key": "key", "project_id": "proj", "app_id": "app"
        })),
        patch.dict("sys.modules", {"firebase_messaging": fake_firebase}),
    ):
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push
        await async_start_fcm_push(coord)

    assert register_called, (
        "fcm_push_mode='auto' must call register_fcm_with_bosch once. "
        "Without registration, Bosch never sends push notifications to this device."
    )


# ── PIN_EVERY_MODE: polling ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_polling_mode_skips_fcm_register() -> None:
    """PIN polling: fcm_push_mode='polling' → register_fcm_with_bosch NOT called.

    Users who explicitly chose 'polling' (e.g. behind FCM-blocking firewalls)
    must never trigger FCM registration. Calling it silently would fail or
    create a registered token the user doesn't want, causing Bosch 500 spam.
    """
    coord = _make_coord(options={"enable_fcm_push": True, "fcm_push_mode": "polling"})

    register_called: list[bool] = []

    async def _fake_register(c: object) -> bool:
        register_called.append(True)
        return True

    with (
        patch(f"{MODULE}.register_fcm_with_bosch", side_effect=_fake_register),
        patch(f"{MODULE}._install_fcm_noise_filter"),
        patch(f"{MODULE}._get_fcm_push_client_class", return_value=MagicMock()),
    ):
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push
        await async_start_fcm_push(coord)

    assert not register_called, (
        "fcm_push_mode='polling' must NOT call register_fcm_with_bosch. "
        "Polling users opted out of FCM — registration would create unwanted "
        "Bosch device entries and cause HTTP 500 spam on each restart."
    )


# ── PIN default ────────────────────────────────────────────────────────────────


def test_default_mode_is_auto() -> None:
    """PIN default: DEFAULT_OPTIONS['fcm_push_mode'] == 'auto'.

    New installs must default to 'auto' so FCM push is attempted automatically.
    A regression to 'polling' would silently downgrade all fresh installs to
    polling-only, increasing motion event latency from ~1s to the polling interval.
    """
    from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
    assert DEFAULT_OPTIONS["fcm_push_mode"] == "auto", (
        "New installs must default to fcm_push_mode='auto' so FCM push is "
        "attempted automatically. Regression to 'polling' would silently "
        "increase motion-event latency for all fresh installs."
    )


# ── PIN garbage / coercion ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_garbage_mode_coerced_to_auto() -> None:
    """PIN garbage: unknown fcm_push_mode string → coerces to 'auto' (FCM path).

    Stale config entries or manual edits can produce arbitrary strings.
    They must not disable FCM silently (which 'polling' would do) — instead
    the integration treats unrecognized values as 'auto' and tries FCM.
    """
    coord = _make_coord(
        options={"enable_fcm_push": True, "fcm_push_mode": "garbage_string"}
    )

    register_called: list[bool] = []

    async def _fake_register(c: object) -> bool:
        register_called.append(True)
        return True

    mock_client = MagicMock()
    mock_client.checkin_or_register = AsyncMock(return_value="fcm-tok")
    mock_client.start = AsyncMock()
    mock_client_cls = MagicMock(return_value=mock_client)

    fake_firebase = MagicMock()
    fake_firebase.FcmRegisterConfig = MagicMock()
    fake_firebase.FcmPushClientConfig = None

    with (
        patch(f"{MODULE}.register_fcm_with_bosch", side_effect=_fake_register),
        patch(f"{MODULE}._install_fcm_noise_filter"),
        patch(f"{MODULE}._get_fcm_push_client_class", return_value=mock_client_cls),
        patch(f"{MODULE}.fetch_firebase_config", new=AsyncMock(return_value={
            "api_key": "key", "project_id": "proj", "app_id": "app"
        })),
        patch.dict("sys.modules", {"firebase_messaging": fake_firebase}),
    ):
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push
        await async_start_fcm_push(coord)

    assert register_called, (
        "Garbage fcm_push_mode must coerce to 'auto' and attempt FCM registration. "
        "Silently treating unknown values as 'polling' would disable push for "
        "users whose config entries contain stale or manually-edited values."
    )


# ── Migration v2 → v3 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_rewrites_ios_to_auto() -> None:
    """Migration v2→v3 must rewrite fcm_push_mode='ios' to 'auto'."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=2, options={"fcm_push_mode": "ios", "enable_fcm_push": True}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto", (
        "Legacy 'ios' must be rewritten to 'auto' — the OSS key handles both "
        "platforms; the iOS-specific code path no longer exists."
    )
    assert captured["version"] == 3


@pytest.mark.asyncio
async def test_migration_rewrites_android_to_auto() -> None:
    """Migration v2→v3 must rewrite fcm_push_mode='android' to 'auto'."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=2, options={"fcm_push_mode": "android", "some_option": True}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto", (
        "Legacy 'android' must be rewritten to 'auto' — the OSS key is already "
        "the Android path; 'android' as a distinct value is now meaningless."
    )
    assert captured["options"]["some_option"] is True, (
        "Other options must be preserved unchanged during migration."
    )
    assert captured["version"] == 3


@pytest.mark.asyncio
async def test_migration_keeps_auto() -> None:
    """Migration v2→v3 must leave fcm_push_mode='auto' unchanged."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=2, options={"fcm_push_mode": "auto"}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto"
    assert captured["version"] == 3


@pytest.mark.asyncio
async def test_migration_keeps_polling() -> None:
    """Migration v2→v3 must leave fcm_push_mode='polling' unchanged.

    Users who explicitly opted out of FCM (firewalls, privacy) must not be
    silently switched to 'auto' during migration.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=2, options={"fcm_push_mode": "polling"}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["options"]["fcm_push_mode"] == "polling", (
        "Explicit 'polling' must survive migration — overwriting with 'auto' "
        "would silently re-enable FCM for users who deliberately disabled it."
    )
    assert captured["version"] == 3


@pytest.mark.asyncio
async def test_migration_v3_entry_is_noop() -> None:
    """A v3 entry must not be touched by the v2→v3 migration."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=3, options={"fcm_push_mode": "auto"}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured == {}, "v3 entry must produce zero async_update_entry calls"
