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
    """Return (hass, entry, captured) for async_migrate_entry tests.

    entry.data defaults to {} so that the v2→v3 migration (which always reads
    entry.data) does not AttributeError on entries that have no FCM data.
    """
    captured: dict = {}

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        captured.update(kwargs)
        if "options" in kwargs:
            entry.options = kwargs["options"]
        if "data" in kwargs:
            entry.data = kwargs["data"]  # type: ignore[assignment]
        if "version" in kwargs:
            entry.version = kwargs["version"]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(
        entry_id="test-entry", version=version, options=options, data={}
    )
    return hass, entry, captured


def _make_migrate_harness_with_data(
    version: int, options: dict, data: dict
) -> tuple:
    """Return (hass, entry, captured) for async_migrate_entry tests that touch entry.data.

    Unlike _make_migrate_harness, the entry carries a data dict and the captured
    dict also records the 'data' kwarg passed to async_update_entry.
    """
    captured: dict = {}

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        captured.update(kwargs)
        if "options" in kwargs:
            entry.options = kwargs["options"]  # type: ignore[assignment]
        if "data" in kwargs:
            entry.data = kwargs["data"]  # type: ignore[assignment]
        if "version" in kwargs:
            entry.version = kwargs["version"]  # type: ignore[assignment]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(
        entry_id="test-entry", version=version, options=options, data=data
    )
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


# ── FCM-cred clearance tests (regression: v2→v3 push-routing break) ───────────
#
# Bug (v12.4.5): async_migrate_entry v2→v3 coerced fcm_push_mode ios/android→auto
# but left fcm_credentials + fcm_registered_token intact in entry.data.
# register_fcm_with_bosch saw "token unchanged" and skipped re-registration, so
# Bosch CBS kept deviceType=IOS while the HA client registered platform=ANDROID
# at Firebase. Push routing broke for every upgrader on legacy ios/android/auto mode.
# Fix: pop fcm_credentials + fcm_registered_token from data whenever fcm_push_mode
# is FCM-bound (ios/android/auto) so re-registration is forced on next startup.


_FCM_DATA_WITH_CREDS: dict = {
    "fcm_credentials": {"token": "old-token-abc", "device_id": "dev-123"},
    "fcm_registered_token": "old-fcm-reg-token",
    "fcm_config": {"api_key": "key", "project_id": "proj"},
    "bearer_token": "bearer-xyz",
    "refresh_token": "refresh-xyz",
}


@pytest.mark.asyncio
async def test_migration_v3_ios_clears_fcm_creds() -> None:
    """(a) fcm_push_mode='ios' + creds present → after v2→v3: mode='auto',
    fcm_credentials + fcm_registered_token absent from data, other data preserved,
    version=3.

    Regression: without the fix, register_fcm_with_bosch saw token unchanged and
    skipped re-registration, leaving Bosch CBS with deviceType=IOS while the
    HA client registered platform=ANDROID. Push routing was silently broken.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=2,
        options={"fcm_push_mode": "ios", "enable_fcm_push": True},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto"
    assert captured["version"] == 3
    assert "fcm_credentials" not in captured["data"], (
        "fcm_credentials must be cleared from data so register_fcm_with_bosch "
        "forces re-registration with deviceType=ANDROID on next startup."
    )
    assert "fcm_registered_token" not in captured["data"], (
        "fcm_registered_token must be cleared to trigger Bosch CBS re-registration."
    )
    # Non-FCM data must survive migration unchanged
    assert captured["data"]["fcm_config"] == _FCM_DATA_WITH_CREDS["fcm_config"]
    assert captured["data"]["bearer_token"] == _FCM_DATA_WITH_CREDS["bearer_token"]
    assert captured["data"]["refresh_token"] == _FCM_DATA_WITH_CREDS["refresh_token"]


@pytest.mark.asyncio
async def test_migration_v3_android_clears_fcm_creds() -> None:
    """(b) fcm_push_mode='android' → same clearance outcome as (a).

    'android' in v2 used the same iOS-first Bosch registration path; after
    migration to the OSS Android key, the old token is stale.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=2,
        options={"fcm_push_mode": "android", "enable_fcm_push": True},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto"
    assert captured["version"] == 3
    assert "fcm_credentials" not in captured["data"]
    assert "fcm_registered_token" not in captured["data"]
    assert captured["data"]["fcm_config"] == _FCM_DATA_WITH_CREDS["fcm_config"]
    assert captured["data"]["bearer_token"] == _FCM_DATA_WITH_CREDS["bearer_token"]


@pytest.mark.asyncio
async def test_migration_v3_old_auto_clears_fcm_creds() -> None:
    """(c) fcm_push_mode='auto' in v2 → creds must also be cleared.

    In v2, 'auto' was an iOS-first chain (not the OSS Android key). Any token
    registered under the old 'auto' is equally stale and must be cleared to
    force re-registration with platform=ANDROID.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=2,
        options={"fcm_push_mode": "auto", "enable_fcm_push": True},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto"
    assert captured["version"] == 3
    assert "fcm_credentials" not in captured["data"], (
        "Old 'auto' (iOS-first) tokens are stale after migration to OSS Android key; "
        "fcm_credentials must be cleared."
    )
    assert "fcm_registered_token" not in captured["data"]


@pytest.mark.asyncio
async def test_migration_v3_polling_preserves_fcm_creds() -> None:
    """(d) fcm_push_mode='polling' → fcm_credentials + fcm_registered_token preserved.

    'polling' users never use FCM; their data dict may contain leftover creds
    from a prior FCM phase but we must not alter them — we must not corrupt data
    that was stored by a different mechanism, and we must not trigger any re-reg.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=2,
        options={"fcm_push_mode": "polling"},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["options"]["fcm_push_mode"] == "polling"
    assert captured["version"] == 3
    assert captured["data"]["fcm_credentials"] == _FCM_DATA_WITH_CREDS["fcm_credentials"], (
        "polling mode: fcm_credentials must NOT be cleared — user opted out of FCM "
        "and we must not silently alter their stored data."
    )
    assert captured["data"]["fcm_registered_token"] == _FCM_DATA_WITH_CREDS["fcm_registered_token"]


@pytest.mark.asyncio
async def test_migration_already_v3_data_untouched() -> None:
    """(e) entry already at version=3 → no-op, data fields not touched."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=3,
        options={"fcm_push_mode": "auto"},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured == {}, (
        "v3 entry must produce zero async_update_entry calls — no options, "
        "no data, no version changes."
    )


@pytest.mark.asyncio
async def test_migration_v1_to_v3_ios_clears_fcm_creds() -> None:
    """(f) entry at version=1 → v1→v2 runs (stream_connection_type preserved)
    THEN v2→v3 clears FCM creds as in test (a).

    v1 entries never had an explicit stream_connection_type, so v1→v2 must
    inject 'auto'. The combined v1→v3 path must also clear FCM creds.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=1,
        options={"fcm_push_mode": "ios", "enable_fcm_push": True},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["version"] == 3
    assert captured["options"]["stream_connection_type"] == "auto", (
        "v1→v2 must inject stream_connection_type='auto' for legacy entries."
    )
    assert captured["options"]["fcm_push_mode"] == "auto", (
        "v2→v3 must coerce 'ios' to 'auto'."
    )
    assert "fcm_credentials" not in captured["data"], (
        "Combined v1→v3 path must clear fcm_credentials."
    )
    assert "fcm_registered_token" not in captured["data"]
    assert captured["data"]["bearer_token"] == _FCM_DATA_WITH_CREDS["bearer_token"]
