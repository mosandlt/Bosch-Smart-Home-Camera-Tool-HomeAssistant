"""`async_setup_entry` go2rtc auto-create + v8.0.2 entity-enable migration.

Covers two blocks inside `async_setup_entry` (`__init__.py`):
  - lines 4383-4395: v8.0.2 migration loop. Entities that were created with
    `disabled_by=INTEGRATION` in older builds (front_light_*, wallwasher_*,
    front_light_intensity_*) must be re-enabled on setup.
  - lines 4404-4422: go2rtc auto-create. If `enable_go2rtc=True` and no
    `go2rtc` config entry exists, `flow.async_init` must fire. If one is
    already active, the flow must NOT be invoked (no duplicates).

Strategy: patch `BoschCameraCoordinator` to a stub with controllable `data`,
then drive `async_setup_entry` end-to-end. Mock cf_unbuffer, platform
forwards, and entity_registry so we only exercise the two blocks of interest.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"
CAM_A = "11111111-1111-1111-1111-111111111111"


def _make_coord_stub(camera_ids):
    """Stub BoschCameraCoordinator — only the attributes accessed by
    async_setup_entry need to exist."""
    coord = MagicMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    coord.data = {cid: {} for cid in camera_ids}
    coord._schedule_token_refresh = MagicMock()
    coord._renewal_tasks = {}
    coord._bg_tasks = set()
    coord._tls_proxy_ports = {}
    coord._nvr_drain_task = None
    coord._token_refresh_handle = None
    coord._stream_log_listener = None
    return coord


def _make_entry(options=None):
    entry = MagicMock()
    entry.options = options or {}
    entry.data = {"bearer_token": "tok", "refresh_token": "ref"}
    entry.entry_id = "test_entry_id"
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=lambda: None)
    return entry


def _make_hass(go2rtc_entries=None):
    """Hass with everything `async_setup_entry` touches stubbed out."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_entries = MagicMock(return_value=go2rtc_entries or [])
    hass.config_entries.flow.async_init = AsyncMock(
        return_value={"type": "create_entry"}
    )
    hass.async_create_task = MagicMock()
    hass.async_create_background_task = MagicMock()
    hass.bus.async_listen_once = MagicMock(return_value=lambda: None)
    return hass


# ── go2rtc auto-create ──────────────────────────────────────────────────────


class TestGo2rtcAutoCreate:
    @pytest.mark.asyncio
    async def test_no_existing_entry_triggers_flow_init(self):
        """No go2rtc config entry exists → `flow.async_init("go2rtc", ...)`
        must fire to bootstrap WebRTC support."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass(go2rtc_entries=[])
        entry = _make_entry()
        coord_stub = _make_coord_stub([])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        hass.config_entries.flow.async_init.assert_awaited_once()
        args, kwargs = hass.config_entries.flow.async_init.await_args
        assert args[0] == "go2rtc"
        assert kwargs["context"] == {"source": "system"}

    @pytest.mark.asyncio
    async def test_existing_entry_skips_flow_init(self):
        """A go2rtc config entry already exists → `flow.async_init` must
        NOT be called (would create a duplicate entry)."""
        from custom_components.bosch_shc_camera import async_setup_entry

        existing = SimpleNamespace(entry_id="already_there")
        hass = _make_hass(go2rtc_entries=[existing])
        entry = _make_entry()
        coord_stub = _make_coord_stub([])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            await async_setup_entry(hass, entry)

        hass.config_entries.flow.async_init.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_via_options_skips_flow_init(self):
        """`enable_go2rtc=False` → block must short-circuit; flow not called."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass(go2rtc_entries=[])
        entry = _make_entry(options={"enable_go2rtc": False})
        coord_stub = _make_coord_stub([])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            await async_setup_entry(hass, entry)

        hass.config_entries.flow.async_init.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flow_init_exception_swallowed(self):
        """`flow.async_init` raising must NOT propagate — setup continues."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass(go2rtc_entries=[])
        hass.config_entries.flow.async_init = AsyncMock(
            side_effect=RuntimeError("nope")
        )
        entry = _make_entry()
        coord_stub = _make_coord_stub([])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            # Must NOT raise — setup-entry continues
            result = await async_setup_entry(hass, entry)
        assert result is True

    @pytest.mark.asyncio
    async def test_abort_type_logged_not_raised(self):
        """`flow.async_init` returning `{"type": "abort"}` is a normal
        outcome (entry already in setup) — must not raise."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass(go2rtc_entries=[])
        hass.config_entries.flow.async_init = AsyncMock(return_value={"type": "abort"})
        entry = _make_entry()
        coord_stub = _make_coord_stub([])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            result = await async_setup_entry(hass, entry)
        assert result is True


# ── v8.0.2 entity-enable migration ──────────────────────────────────────────


class TestV802Migration:
    @pytest.mark.asyncio
    async def test_disabled_by_integration_entries_re_enabled(self):
        """An entity registered with `disabled_by=INTEGRATION` in an older
        build (v<8.0.2) must be re-enabled via `async_update_entity(...,
        disabled_by=None)` during setup."""
        from homeassistant.helpers import entity_registry as er

        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass(go2rtc_entries=[SimpleNamespace(entry_id="x")])  # skip go2rtc
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])

        # Each lookup returns a fake entity-id; the entry has the disabled flag set
        fake_entries = {}

        def _entity_id(domain, integration, uid):
            eid = f"{domain}.fake_{uid}"
            fake_entries[eid] = SimpleNamespace(
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
            return eid

        def _get(eid):
            return fake_entries.get(eid)

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(side_effect=_entity_id)
        ent_reg.async_get = MagicMock(side_effect=_get)
        ent_reg.async_update_entity = MagicMock()

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            await async_setup_entry(hass, entry)

        # 3 uid_suffixes × 1 camera = 3 updates
        assert ent_reg.async_update_entity.call_count == 3
        for call in ent_reg.async_update_entity.call_args_list:
            assert call.kwargs.get("disabled_by") is None, (
                "Migration must re-enable, not leave disabled"
            )

    @pytest.mark.asyncio
    async def test_entries_disabled_by_user_left_alone(self):
        """If a user explicitly disabled the entity (`disabled_by=USER`), the
        migration must NOT override their choice."""
        from homeassistant.helpers import entity_registry as er

        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass(go2rtc_entries=[SimpleNamespace(entry_id="x")])
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])

        def _entity_id(domain, integration, uid):
            return f"{domain}.fake_{uid}"

        def _get(eid):
            # User-disabled — must NOT be re-enabled
            return SimpleNamespace(disabled_by=er.RegistryEntryDisabler.USER)

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(side_effect=_entity_id)
        ent_reg.async_get = MagicMock(side_effect=_get)
        ent_reg.async_update_entity = MagicMock()

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            await async_setup_entry(hass, entry)

        ent_reg.async_update_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_entity_skipped(self):
        """Entity not in registry (fresh install) → no update call,
        no crash."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass(go2rtc_entries=[SimpleNamespace(entry_id="x")])
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)
        ent_reg.async_update_entity = MagicMock()

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        ent_reg.async_update_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_intensity_uses_number_platform(self):
        """`front_light_intensity_*` is a `number` entity; others are
        `switch`. The lookup must use the correct platform string."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass(go2rtc_entries=[SimpleNamespace(entry_id="x")])
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])

        seen_platforms = []

        def _entity_id(domain, integration, uid):
            seen_platforms.append((domain, uid))
            return None

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(side_effect=_entity_id)

        with (
            patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
            patch(f"{MODULE}.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=ent_reg
            ),
        ):
            await async_setup_entry(hass, entry)

        # Find lookups for the intensity uid — must be on `number` platform
        intensity_lookups = [p for p, uid in seen_platforms if "intensity" in uid]
        assert intensity_lookups, "intensity uid must be looked up"
        assert all(p == "number" for p in intensity_lookups), (
            "intensity entities live on the `number` platform, not `switch`"
        )
        switch_lookups = [
            p
            for p, uid in seen_platforms
            if "intensity" not in uid
            and ("front_light_" in uid or "wallwasher_" in uid)
        ]
        assert switch_lookups
        assert all(p == "switch" for p in switch_lookups), (
            "front_light / wallwasher live on the `switch` platform"
        )
