"""Sprint MD — coverage gap-fill for additions in v12.6.0.

Targets (after 2026-05-20 additions):
  __init__.py
    2523-2527   _hw_version_store persist path in coordinator update
    2535-2548   _local_creds_store persist path in coordinator update
    2710-2714   _persist_maint_notified_key (store+key both set)
    2722-2725   _persist_cloud_outage_flag (store set)
    5328-5329   async_setup_entry: loaded maint-key dedup logging path
    5342-5343   async_setup_entry: cloud-outage flag restored to True
    5399-5407   async_setup_entry: creds loaded from store into cache
    5409        async_setup_entry: _loaded_creds > 0 → _LOGGER.info
    5431-5439   async_setup_entry: device-registry hw-version recovery
    5520-5521   async_setup_entry: Indoor-II orphan migration: inner-loop body
    5531        async_setup_entry: orphan has Indoor-II cam_id → append
    5534        async_setup_entry: _LOGGER.info on removal
    5536-5540   async_setup_entry: _ereg.async_remove called per orphan
    5672-5703   _async_deliver_webhook closure (actual production code)
    5715-5747   handle_send_event_webhook closure (actual production code)
    5750        hass.services.async_register called for send_event_webhook
  rcp.py
    273-277     rcp_local_write Digest auth: non-200 → _LOGGER.debug + return False
    280-284     rcp_local_write Digest auth: <err> in body → _LOGGER.debug + return False
  select.py
    506-508     BoschPanPresetSelect.device_info property
  switch.py
    283-284     BoschLiveStreamSwitch.async_added_to_hass body
    287-288     BoschLiveStreamSwitch.async_will_remove_from_hass body

All tests use unbound-method or minimal-stub patterns — no live HA runtime.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"
CAM_A = "11111111-1111-1111-1111-111111111111"
CAM_B = "22222222-AAAA-BBBB-CCCC-000000000002"


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers (mirrors test_setup_entry_lan_fallback.py patterns)
# ─────────────────────────────────────────────────────────────────────────────


class _FakeStore:
    """A Store stub whose async_load returns a preset payload."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.saved: list[Any] = []

    async def async_load(self) -> Any:
        return self._payload

    async def async_save(self, data: Any) -> None:
        self.saved.append(data)


class _MultiStore:
    """Factory that returns per-key _FakeStore instances.

    Pass `payloads={key_suffix: payload}` where key_suffix matches the
    trailing part of the Store `key=` kwarg.
    """

    def __init__(self, payloads: dict[str, Any]) -> None:
        self._payloads = payloads
        self.stores: dict[str, _FakeStore] = {}

    def __call__(self, hass: Any, *, version: int, key: str) -> "_FakeStore":
        """Called as Store(hass, version=1, key=...)."""
        for suffix, payload in self._payloads.items():
            if key.endswith(suffix):
                store = _FakeStore(payload)
                self.stores[key] = store
                return store
        store = _FakeStore(None)
        self.stores[key] = store
        return store


def _make_coord_stub(camera_ids: list[str], *, first_refresh_raises: Any = None) -> MagicMock:
    coord = MagicMock()
    if first_refresh_raises is not None:
        coord.async_config_entry_first_refresh = AsyncMock(side_effect=first_refresh_raises)
    else:
        coord.async_config_entry_first_refresh = AsyncMock()
    coord.data = {cid: {} for cid in camera_ids}
    coord._rcp_lan_ip_cache = {}
    coord._hw_version = {}
    coord._local_creds_cache = {}
    coord._cloud_outage_notified = False
    coord._maintenance_notified_key = None
    coord._schedule_token_refresh = MagicMock()
    coord._renewal_tasks = {}
    coord._bg_tasks = set()
    coord._tls_proxy_ports = {}
    coord._nvr_drain_task = None
    coord._token_refresh_handle = None
    coord._stream_log_listener = None
    coord._async_outage_ping_all = AsyncMock(return_value=None)
    coord.async_start_fcm_push = AsyncMock(return_value=None)
    return coord


def _make_entry(options: dict[str, Any] | None = None) -> MagicMock:
    entry = MagicMock()
    entry.options = options or {}
    entry.data = {"bearer_token": "tok", "refresh_token": "ref"}
    entry.entry_id = "test_entry_id"
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=lambda: None)
    return entry


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_entries = MagicMock(return_value=[])
    hass.config_entries.flow.async_init = AsyncMock(return_value={"type": "create_entry"})
    hass.async_create_task = MagicMock()
    hass.async_create_background_task = MagicMock()
    hass.bus.async_listen_once = MagicMock(return_value=lambda: None)
    hass.bus.async_listen = MagicMock(return_value=lambda: None)
    hass.services.has_service = MagicMock(return_value=False)
    hass.services.async_register = MagicMock()
    hass.states = MagicMock()
    return hass


def _make_ent_reg() -> MagicMock:
    ent_reg = MagicMock()
    ent_reg.async_get_entity_id = MagicMock(return_value=None)
    return ent_reg


# ─────────────────────────────────────────────────────────────────────────────
# rcp.py — rcp_local_write Digest auth error paths (lines 273-284)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRcpLocalWriteDigestErrors:
    """rcp_local_write with user+password — non-200 and <err> body paths."""

    def _make_digest_ctx(self, status: int, body: bytes) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.read = AsyncMock(return_value=body)
        resp_ctx = MagicMock()
        resp_ctx.__aenter__ = AsyncMock(return_value=resp)
        resp_ctx.__aexit__ = AsyncMock(return_value=None)
        return resp_ctx

    async def test_digest_non_200_returns_false(self) -> None:
        """Digest path: HTTP 403 → _LOGGER.debug + return False. Pins L273-277."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        resp_ctx = self._make_digest_ctx(403, b"<forbidden/>")

        # The function does `from .auth_utils import async_digest_request` at runtime.
        # Patch at the auth_utils module level so the conditional local import picks it up.
        with patch(
            "custom_components.bosch_shc_camera.auth_utils.async_digest_request",
            AsyncMock(return_value=resp_ctx),
        ), patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=MagicMock(),
        ):
            result = await rcp_local_write(
                MagicMock(),
                "192.0.2.149",
                "0x0c22",
                "0xdeadbeef",
                user="cbs-XXXXXXXX",
                password="secret",
            )

        assert result is False

    async def test_digest_err_in_body_returns_false(self) -> None:
        """Digest path: HTTP 200 but body contains <err> → return False. Pins L280-284."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        resp_ctx = self._make_digest_ctx(200, b"<err>auth failed</err>")

        with patch(
            "custom_components.bosch_shc_camera.auth_utils.async_digest_request",
            AsyncMock(return_value=resp_ctx),
        ), patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=MagicMock(),
        ):
            result = await rcp_local_write(
                MagicMock(),
                "192.0.2.149",
                "0x0c22",
                "0xdeadbeef",
                user="cbs-XXXXXXXX",
                password="secret",
            )

        assert result is False

    async def test_digest_200_no_err_returns_true(self) -> None:
        """Digest path happy-path: HTTP 200, clean body → return True (baseline)."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        resp_ctx = self._make_digest_ctx(200, b"<result>OK</result>")

        with patch(
            "custom_components.bosch_shc_camera.auth_utils.async_digest_request",
            AsyncMock(return_value=resp_ctx),
        ), patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=MagicMock(),
        ):
            result = await rcp_local_write(
                MagicMock(),
                "192.0.2.149",
                "0x0c22",
                "0xdeadbeef",
                user="cbs-XXXXXXXX",
                password="secret",
            )

        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# select.py — BoschPanPresetSelect.device_info (lines 506-508)
# ─────────────────────────────────────────────────────────────────────────────


class TestPanPresetDeviceInfo:
    """BoschPanPresetSelect.device_info returns correct identifiers dict."""

    def test_device_info_identifiers(self) -> None:
        """device_info must include (DOMAIN, cam_id) in identifiers. Pins L506-508."""
        from custom_components.bosch_shc_camera.select import BoschPanPresetSelect
        from custom_components.bosch_shc_camera.const import DOMAIN

        coord = SimpleNamespace(
            data={
                CAM_A: {
                    "info": {
                        "title": "Kamera",
                        "hardwareVersion": "CAMERA_360",
                        "firmwareVersion": "7.91.56",
                    }
                }
            },
            last_update_success=True,
            async_cloud_set_pan=AsyncMock(return_value=True),
            _pan_cache={CAM_A: 0},
            _image_rotation_180={},
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        sel = BoschPanPresetSelect(coord, CAM_A, entry, pan_limit=120)

        info = sel.device_info
        assert (DOMAIN, CAM_A) in info["identifiers"]
        assert info["manufacturer"] == "Bosch"
        assert info["model"] == "CAMERA_360"
        assert info["sw_version"] == "7.91.56"

    def test_device_info_fallback_model(self) -> None:
        """device_info model falls back to 'Smart Home Camera' when hardwareVersion absent."""
        from custom_components.bosch_shc_camera.select import BoschPanPresetSelect

        coord = SimpleNamespace(
            data={CAM_A: {"info": {"title": "Kamera"}}},
            last_update_success=True,
            async_cloud_set_pan=AsyncMock(return_value=True),
            _pan_cache={},
            _image_rotation_180={},
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        sel = BoschPanPresetSelect(coord, CAM_A, entry, pan_limit=120)

        info = sel.device_info
        assert info["model"] == "Smart Home Camera"
        assert info["sw_version"] == ""


# ─────────────────────────────────────────────────────────────────────────────
# switch.py — BoschLiveStreamSwitch lifecycle (lines 283-284, 287-288)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestLiveStreamSwitchLifecycle:
    """async_added_to_hass + async_will_remove_from_hass of BoschLiveStreamSwitch."""

    async def test_async_added_registers_entity_in_coordinator(self) -> None:
        """async_added_to_hass must store self in coordinator._live_stream_entities. Pins L283-284."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            data={CAM_A: {"info": {"title": "T", "hardwareVersion": "HOME_Eyes_Outdoor"}, "status": "ONLINE"}},
            _live_connections={},
            _user_intent_streams=set(),
            _shc_state_cache={CAM_A: {"privacy_mode": False}},
            _session_stale={},
            _stream_warming=set(),
            _privacy_set_at={},
            _light_set_at={},
            _audio_enabled={CAM_A: True},
            _privacy_sound_cache={CAM_A: False},
            _timestamp_cache={CAM_A: True},
            _ledlights_cache={CAM_A: True},
            _arming_cache={},
            _rcp_privacy_cache={},
            last_update_success=True,
            options={},
            is_camera_online=lambda cid: True,
            is_session_stale=lambda cid: False,
            is_stream_warming=lambda cid: False,
            _live_stream_entities={},
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={"bearer_token": "x"}, options={})
        sw = BoschLiveStreamSwitch(coord, CAM_A, entry)

        # Patch super().async_added_to_hass to avoid HA runtime dependency
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
            AsyncMock(),
        ):
            await sw.async_added_to_hass()

        assert coord._live_stream_entities[CAM_A] is sw

    async def test_async_will_remove_deregisters_entity(self) -> None:
        """async_will_remove_from_hass must pop self from _live_stream_entities. Pins L287-288."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            data={CAM_A: {"info": {"title": "T", "hardwareVersion": "HOME_Eyes_Outdoor"}, "status": "ONLINE"}},
            _live_connections={},
            _user_intent_streams=set(),
            _shc_state_cache={CAM_A: {"privacy_mode": False}},
            _session_stale={},
            _stream_warming=set(),
            _privacy_set_at={},
            _light_set_at={},
            _audio_enabled={CAM_A: True},
            _privacy_sound_cache={CAM_A: False},
            _timestamp_cache={CAM_A: True},
            _ledlights_cache={CAM_A: True},
            _arming_cache={},
            _rcp_privacy_cache={},
            last_update_success=True,
            options={},
            is_camera_online=lambda cid: True,
            is_session_stale=lambda cid: False,
            is_stream_warming=lambda cid: False,
            _live_stream_entities={CAM_A: MagicMock()},  # pre-populate
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={"bearer_token": "x"}, options={})
        sw = BoschLiveStreamSwitch(coord, CAM_A, entry)

        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_will_remove_from_hass",
            AsyncMock(),
        ):
            await sw.async_will_remove_from_hass()

        assert CAM_A not in coord._live_stream_entities


# ─────────────────────────────────────────────────────────────────────────────
# __init__.py — coordinator methods _persist_maint_notified_key (L2710-2714)
# and _persist_cloud_outage_flag (L2722-2725)
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistMethods:
    """Direct calls to coordinator persist helpers via unbound-method pattern."""

    def test_persist_maint_key_with_store_and_key(self) -> None:
        """_persist_maint_notified_key: store+key both set → async_create_task called. Pins L2710-2714."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        store = MagicMock()
        store.async_save = AsyncMock()

        coord = SimpleNamespace(
            hass=MagicMock(),
            _maintenance_notified_key=("https://example.com/maint", "ACTIVE"),
            _maint_notified_store=store,
        )
        BoschCameraCoordinator._persist_maint_notified_key(coord)  # type: ignore[arg-type]

        # hass.async_create_task should have been called with the coroutine
        coord.hass.async_create_task.assert_called_once()

    def test_persist_maint_key_no_store(self) -> None:
        """_persist_maint_notified_key: store=None → early return, no async_create_task."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = SimpleNamespace(
            hass=MagicMock(),
            _maintenance_notified_key=("https://example.com/maint", "ACTIVE"),
            # no _maint_notified_store attribute → getattr returns None
        )
        BoschCameraCoordinator._persist_maint_notified_key(coord)  # type: ignore[arg-type]

        coord.hass.async_create_task.assert_not_called()

    def test_persist_maint_key_no_key(self) -> None:
        """_persist_maint_notified_key: key=None → early return."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        store = MagicMock()
        coord = SimpleNamespace(
            hass=MagicMock(),
            _maintenance_notified_key=None,
            _maint_notified_store=store,
        )
        BoschCameraCoordinator._persist_maint_notified_key(coord)  # type: ignore[arg-type]

        coord.hass.async_create_task.assert_not_called()

    def test_persist_cloud_outage_with_store(self) -> None:
        """_persist_cloud_outage_flag: store set → async_create_task called. Pins L2722-2725."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        store = MagicMock()
        store.async_save = AsyncMock()

        coord = SimpleNamespace(
            hass=MagicMock(),
            _cloud_outage_notified=True,
            _cloud_alert_store=store,
        )
        BoschCameraCoordinator._persist_cloud_outage_flag(coord)  # type: ignore[arg-type]

        coord.hass.async_create_task.assert_called_once()

    def test_persist_cloud_outage_no_store(self) -> None:
        """_persist_cloud_outage_flag: store=None → early return."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = SimpleNamespace(
            hass=MagicMock(),
            _cloud_outage_notified=False,
            # no _cloud_alert_store attribute
        )
        BoschCameraCoordinator._persist_cloud_outage_flag(coord)  # type: ignore[arg-type]

        coord.hass.async_create_task.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# __init__.py coordinator update — hw_version_store + local_creds_store persist
# (lines 2523-2527, 2535-2548)
# These live inside _async_update_data — tested via unbound-method pattern
# with a full coordinator stub.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCoordinatorPersistOnUpdate:
    """Coordinator _async_update_data persists hw_version and local creds."""

    async def test_hw_version_persisted_on_first_change(self) -> None:
        """hw_version_store.async_save called when hw_version changes. Pins L2523-2527."""
        import threading
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from .test_init_sprint_ka import _make_coord, _make_resp, _make_session, _PATCH_SESSION

        hw_store = MagicMock()
        hw_store.async_save = AsyncMock()

        coord = _make_coord()
        coord._first_tick_done = True
        coord._async_maybe_announce_camera_status = AsyncMock(return_value=None)
        coord._compute_status_for = MagicMock(return_value="online")
        coord._async_refresh_maintenance = AsyncMock(return_value=None)
        coord._async_maybe_announce_cloud_state = AsyncMock(return_value=None)
        coord._MAINTENANCE_INTERVAL_S = 3600.0
        coord._maintenance_last_fetch = float("-inf")
        coord._maintenance_cache = None
        coord._lan_ips_store = MagicMock()
        coord._lan_ips_store.async_save = AsyncMock()
        coord._lan_ips_snapshot = None
        # Add hw_version_store with a DIFFERENT snapshot so save fires
        coord._hw_version = {CAM_A: "HOME_Eyes_Outdoor"}
        coord._hw_version_store = hw_store
        coord._hw_version_snapshot = None  # different from current → will save
        coord._local_creds_store = None    # skip creds path

        session = _make_session({
            "v11/video_inputs": _make_resp(200, [{"id": CAM_A, "title": "Terrasse"}]),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            "ping": _make_resp(200, {}, text_data="ONLINE"),
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        hw_store.async_save.assert_called_once()
        saved = hw_store.async_save.call_args.args[0]
        assert CAM_A in saved

    async def test_local_creds_persisted_on_change(self) -> None:
        """_local_creds_store.async_save called when creds change. Pins L2535-2548."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from .test_init_sprint_ka import _make_coord, _make_resp, _make_session, _PATCH_SESSION

        creds_store = MagicMock()
        creds_store.async_save = AsyncMock()

        coord = _make_coord()
        coord._first_tick_done = True
        coord._async_maybe_announce_camera_status = AsyncMock(return_value=None)
        coord._compute_status_for = MagicMock(return_value="online")
        coord._async_refresh_maintenance = AsyncMock(return_value=None)
        coord._async_maybe_announce_cloud_state = AsyncMock(return_value=None)
        coord._MAINTENANCE_INTERVAL_S = 3600.0
        coord._maintenance_last_fetch = float("-inf")
        coord._maintenance_cache = None
        coord._lan_ips_store = MagicMock()
        coord._lan_ips_store.async_save = AsyncMock()
        coord._lan_ips_snapshot = None
        coord._hw_version_store = None   # skip hw path
        # Wire creds cache with a valid entry and no prior snapshot
        coord._local_creds_cache = {
            CAM_A: {
                "user": "cbs-XXXXXXXX",
                "password": "s3cr3t",
                "host": "192.0.2.149",
                "port": 443,
                "ts": 123.0,
            }
        }
        coord._local_creds_store = creds_store
        coord._local_creds_snapshot = None  # different from current → will save

        session = _make_session({
            "v11/video_inputs": _make_resp(200, [{"id": CAM_A, "title": "Terrasse"}]),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            "ping": _make_resp(200, {}, text_data="ONLINE"),
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        creds_store.async_save.assert_called_once()
        saved = creds_store.async_save.call_args.args[0]
        assert CAM_A in saved
        assert saved[CAM_A]["user"] == "cbs-XXXXXXXX"
        assert "ts" not in saved[CAM_A]  # ts is stripped (only user/password/host/port saved)


# ─────────────────────────────────────────────────────────────────────────────
# async_setup_entry — persisted-store load paths (lines 5328-5329, 5342-5343,
# 5399-5407, 5409, 5431-5439, 5520-5521, 5531, 5534, 5536-5540)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSetupEntryPersistedStores:
    """Drive async_setup_entry with non-trivial Store payloads to hit logging paths."""

    async def test_maint_key_loaded_from_store(self) -> None:
        """Persisted maint-notify key dict → coordinator._maintenance_notified_key set.
        Pins L5328-5329."""
        from custom_components.bosch_shc_camera import async_setup_entry

        maint_payload = {"link": "https://example.com/maint/12345", "state": "ACTIVE"}
        store_factory = _MultiStore(
            {
                "_maint_notified": maint_payload,
                "_cloud_alert_state": None,
                "_lan_ips": None,
                "_hw_versions": None,
                "_local_creds": None,
            }
        )

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"}}}

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=_make_ent_reg()):
            result = await async_setup_entry(hass, entry)

        assert result is True
        # coordinator._maintenance_notified_key should be a tuple (link, state)
        assert coord_stub._maintenance_notified_key == ("https://example.com/maint/12345", "ACTIVE")

    async def test_cloud_outage_flag_loaded_from_store(self) -> None:
        """Persisted cloud-outage flag True → coordinator._cloud_outage_notified=True.
        Pins L5342-5343."""
        from custom_components.bosch_shc_camera import async_setup_entry

        store_factory = _MultiStore(
            {
                "_maint_notified": None,
                "_cloud_alert_state": {"outage_notified": True},
                "_lan_ips": None,
                "_hw_versions": None,
                "_local_creds": None,
            }
        )

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=_make_ent_reg()):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert coord_stub._cloud_outage_notified is True

    async def test_local_creds_loaded_from_store(self) -> None:
        """Persisted local creds with user+password+host → cached in coordinator.
        Pins L5399-5407, L5409."""
        from custom_components.bosch_shc_camera import async_setup_entry
        import time

        creds_payload = {
            CAM_A.lower(): {
                "user": "cbs-DEADBEEF",
                "password": "s3cr3t",
                "host": "192.0.2.149",
                "port": 443,
            },
            "bad_entry": {"user": "x"},   # missing password+host → skipped
            "also_bad": "not_a_dict",     # wrong type → skipped
        }

        store_factory = _MultiStore(
            {
                "_maint_notified": None,
                "_cloud_alert_state": None,
                "_lan_ips": None,
                "_hw_versions": None,
                "_local_creds": creds_payload,
            }
        )

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=_make_ent_reg()):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert CAM_A in coord_stub._local_creds_cache
        cached = coord_stub._local_creds_cache[CAM_A]
        assert cached["user"] == "cbs-DEADBEEF"
        assert cached["password"] == "s3cr3t"
        assert cached["host"] == "192.0.2.149"
        assert cached["port"] == 443

    async def test_hw_version_recovered_from_device_registry(self) -> None:
        """Device registry: device with matching MODELS display_name → hw_version recovered.
        Pins L5431-5439 (including the _LOGGER.info at L5438-5439)."""
        from custom_components.bosch_shc_camera import async_setup_entry
        from custom_components.bosch_shc_camera.const import DOMAIN

        store_factory = _MultiStore(
            {
                "_maint_notified": None,
                "_cloud_alert_state": None,
                "_lan_ips": None,
                "_hw_versions": None,
                "_local_creds": None,
            }
        )

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}
        coord_stub._hw_version = {}  # empty so recovery path runs

        # Model display_name must match a key in MODELS; "Eyes Außenkamera II" → HOME_Eyes_Outdoor
        fake_device = SimpleNamespace(
            identifiers={(DOMAIN, CAM_A)},
            model="Eyes Außenkamera II",
        )
        fake_dreg = MagicMock()

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=_make_ent_reg()), \
             patch("homeassistant.helpers.device_registry.async_get",
                   return_value=fake_dreg), \
             patch("homeassistant.helpers.device_registry.async_entries_for_config_entry",
                   return_value=[fake_device]):
            result = await async_setup_entry(hass, entry)

        assert result is True
        # hw_version should have been recovered from the device registry
        assert coord_stub._hw_version.get(CAM_A) == "HOME_Eyes_Outdoor"

    async def test_hw_version_recovery_skips_wrong_domain_identifier(self) -> None:
        """Device with identifier from a different domain → continue (skip). Pins L5433."""
        from custom_components.bosch_shc_camera import async_setup_entry
        from custom_components.bosch_shc_camera.const import DOMAIN

        store_factory = _MultiStore(
            {k: None for k in ["_maint_notified", "_cloud_alert_state", "_lan_ips", "_hw_versions", "_local_creds"]}
        )

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}
        coord_stub._hw_version = {}

        # Device has identifiers from a different domain — should be skipped
        fake_device = SimpleNamespace(
            identifiers={("some_other_domain", CAM_A), (DOMAIN, CAM_A)},
            model="Eyes Außenkamera II",
        )
        fake_dreg = MagicMock()

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=_make_ent_reg()), \
             patch("homeassistant.helpers.device_registry.async_get",
                   return_value=fake_dreg), \
             patch("homeassistant.helpers.device_registry.async_entries_for_config_entry",
                   return_value=[fake_device]):
            result = await async_setup_entry(hass, entry)

        assert result is True

    async def test_hw_version_recovery_skips_already_populated(self) -> None:
        """Device cam_id already in coordinator._hw_version → continue (skip). Pins L5435."""
        from custom_components.bosch_shc_camera import async_setup_entry
        from custom_components.bosch_shc_camera.const import DOMAIN

        store_factory = _MultiStore(
            {k: None for k in ["_maint_notified", "_cloud_alert_state", "_lan_ips", "_hw_versions", "_local_creds"]}
        )

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}
        # Already populated — should not be overwritten
        coord_stub._hw_version = {CAM_A: "HOME_Eyes_Outdoor"}

        fake_device = SimpleNamespace(
            identifiers={(DOMAIN, CAM_A)},
            model="Eyes Innenkamera II",  # would map to HOME_Eyes_Indoor — should NOT overwrite
        )
        fake_dreg = MagicMock()

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=_make_ent_reg()), \
             patch("homeassistant.helpers.device_registry.async_get",
                   return_value=fake_dreg), \
             patch("homeassistant.helpers.device_registry.async_entries_for_config_entry",
                   return_value=[fake_device]):
            result = await async_setup_entry(hass, entry)

        assert result is True
        # Original value must be preserved — the already-populated continue path was taken
        assert coord_stub._hw_version[CAM_A] == "HOME_Eyes_Outdoor"


# ─────────────────────────────────────────────────────────────────────────────
# async_setup_entry — Indoor II orphan entity migration (L5520-5540)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestIndoorIIOrphanMigration:
    """v12.5.1 migration removes orphan light entities for Indoor II cameras."""

    async def test_orphan_front_light_entity_removed_for_indoor_ii(self) -> None:
        """Indoor II camera with orphan _front_light_entity → async_remove called.
        Pins L5520-5521, L5531, L5534, L5536-5540."""
        from custom_components.bosch_shc_camera import async_setup_entry

        CAM_INDOOR = "22222222-BBBB-CCCC-DDDD-000000000003"
        store_factory = _MultiStore(
            {
                "_maint_notified": None,
                "_cloud_alert_state": None,
                "_lan_ips": None,
                "_hw_versions": None,
                "_local_creds": None,
            }
        )

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_INDOOR])
        coord_stub.data = {CAM_INDOOR: {"info": {"title": "Indoor", "hardwareVersion": "HOME_Eyes_Indoor"}}}
        coord_stub._hw_version = {CAM_INDOOR: "HOME_Eyes_Indoor"}

        # The orphan entity: unique_id ends with _front_light_entity AND contains Indoor II cam_id
        orphan_uid = f"bosch_shc_camera_{CAM_INDOOR.lower()}_front_light_entity"
        fake_orphan_ent = SimpleNamespace(
            unique_id=orphan_uid,
            entity_id="switch.bosch_indoor_front_light",
        )
        # Non-orphan entity (should be ignored)
        clean_ent = SimpleNamespace(
            unique_id=f"bosch_shc_camera_{CAM_INDOOR.lower()}_privacy",
            entity_id="switch.bosch_indoor_privacy",
        )

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)
        ent_reg.async_remove = MagicMock()

        # er.async_entries_for_config_entry is a module-level function used twice:
        # once for v12.4.10 stale-lan-id check (returns [] — no stale ids)
        # once for v12.5.1 indoor-II orphan check (returns [orphan, clean])
        call_count = [0]
        def _entries_side_effect(reg: Any, entry_id: str) -> list:
            call_count[0] += 1
            if call_count[0] == 1:
                return []  # v12.4.10 migration: no stale LAN ids
            return [fake_orphan_ent, clean_ent]  # v12.5.1 migration

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg), \
             patch("homeassistant.helpers.entity_registry.async_entries_for_config_entry",
                   side_effect=_entries_side_effect):
            result = await async_setup_entry(hass, entry)

        assert result is True
        ent_reg.async_remove.assert_called_once_with("switch.bosch_indoor_front_light")

    async def test_orphan_not_removed_for_outdoor_camera(self) -> None:
        """Same uid suffix on an Outdoor camera → NOT removed (no Indoor II cam_id match)."""
        from custom_components.bosch_shc_camera import async_setup_entry

        CAM_OUTDOOR = "11111111-AAAA-BBBB-CCCC-000000000001"
        store_factory = _MultiStore(
            {
                "_maint_notified": None,
                "_cloud_alert_state": None,
                "_lan_ips": None,
                "_hw_versions": None,
                "_local_creds": None,
            }
        )

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_OUTDOOR])
        coord_stub.data = {CAM_OUTDOOR: {"info": {"title": "Outdoor", "hardwareVersion": "HOME_Eyes_Outdoor"}}}
        coord_stub._hw_version = {CAM_OUTDOOR: "HOME_Eyes_Outdoor"}  # NOT indoor

        orphan_uid = f"bosch_shc_camera_{CAM_OUTDOOR.lower()}_front_light_entity"
        fake_ent = SimpleNamespace(unique_id=orphan_uid, entity_id="switch.bosch_outdoor_front_light")

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)
        ent_reg.async_remove = MagicMock()

        # First call = v12.4.10 migration; second call = v12.5.1 migration
        call_count = [0]
        def _entries_side_effect(reg: Any, entry_id: str) -> list:
            call_count[0] += 1
            if call_count[0] == 1:
                return []
            return [fake_ent]

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg), \
             patch("homeassistant.helpers.entity_registry.async_entries_for_config_entry",
                   side_effect=_entries_side_effect):
            result = await async_setup_entry(hass, entry)

        assert result is True
        # async_remove should NOT have been called — outdoor cam_id not in _indoor_ii_cam_ids
        calls_for_outdoor = [
            c for c in ent_reg.async_remove.call_args_list
            if "outdoor" in str(c)
        ]
        assert len(calls_for_outdoor) == 0


# ─────────────────────────────────────────────────────────────────────────────
# async_setup_entry — webhook delivery closures (L5672-5703, L5715-5747, L5750)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSetupEntryWebhookClosure:
    """Drive async_setup_entry to register the webhook closure, then invoke it."""

    def _make_session_mock(self, status: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.post = MagicMock(return_value=ctx)
        return session

    async def _run_full_test(
        self,
        options: dict[str, Any],
        event: Any,
        *,
        session: MagicMock | None = None,
        session_status: int = 200,
    ) -> tuple[MagicMock, list[Any]]:
        """Run async_setup_entry + invoke captured listener, keep patches active throughout."""
        from custom_components.bosch_shc_camera import async_setup_entry

        store_factory = _MultiStore(
            {
                "_maint_notified": None,
                "_cloud_alert_state": None,
                "_lan_ips": None,
                "_hw_versions": None,
                "_local_creds": None,
            }
        )
        hass = _make_hass()
        captured: list[Any] = []

        def _bus_listen(event_type: str, listener: Any) -> Any:
            captured.append((event_type, listener))
            return MagicMock()

        hass.bus.async_listen = MagicMock(side_effect=_bus_listen)
        entry = _make_entry(options=options)
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}

        if session is None:
            session = self._make_session_mock(session_status)

        # Keep async_get_clientsession patched THROUGH the listener call
        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=_make_ent_reg()), \
             patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_setup_entry(hass, entry)
            listener = next(fn for et, fn in captured if et == "bosch_shc_camera_motion")
            await listener(event)

        return session, captured

    async def test_webhook_listener_registered_for_four_event_types(self) -> None:
        """async_setup_entry registers _async_deliver_webhook for 4 event types. Pins L5707-5710."""
        from custom_components.bosch_shc_camera.const import (
            CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL,
        )
        from custom_components.bosch_shc_camera import async_setup_entry

        store_factory = _MultiStore(
            {k: None for k in ["_maint_notified", "_cloud_alert_state", "_lan_ips", "_hw_versions", "_local_creds"]}
        )
        hass = _make_hass()
        captured: list[Any] = []
        hass.bus.async_listen = MagicMock(side_effect=lambda et, fn: captured.append((et, fn)) or MagicMock())

        entry = _make_entry(options={CONF_ENABLE_WEBHOOK_DELIVERY: False, CONF_WEBHOOK_URL: ""})
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get", return_value=_make_ent_reg()):
            await async_setup_entry(hass, entry)

        event_types = {et for et, _ in captured}
        assert "bosch_shc_camera_motion" in event_types
        assert "bosch_shc_camera_audio_alarm" in event_types
        assert "bosch_shc_camera_person" in event_types
        assert "bosch_shc_camera_intrusion" in event_types

    async def test_deliver_webhook_disabled_no_post(self) -> None:
        """_async_deliver_webhook: webhook disabled → no POST. Pins L5674-5675."""
        from custom_components.bosch_shc_camera.const import (
            CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL,
        )
        ev = SimpleNamespace(
            event_type="bosch_shc_camera_motion",
            data={"camera_id": CAM_A, "camera_name": "T", "timestamp": "2026-01-01T00:00:00Z"},
        )
        session, _ = await self._run_full_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: False, CONF_WEBHOOK_URL: "https://example.com/hook"},
            ev,
        )
        session.post.assert_not_called()

    async def test_deliver_webhook_enabled_empty_url_no_post(self) -> None:
        """_async_deliver_webhook: enabled but URL empty → no POST, warning. Pins L5677-5681."""
        from custom_components.bosch_shc_camera.const import (
            CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL,
        )
        ev = SimpleNamespace(
            event_type="bosch_shc_camera_motion",
            data={"camera_id": CAM_A, "camera_name": "T", "timestamp": "2026-01-01T00:00:00Z"},
        )
        session, _ = await self._run_full_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: ""},
            ev,
        )
        session.post.assert_not_called()

    async def test_deliver_webhook_posts_to_url(self) -> None:
        """_async_deliver_webhook: enabled + URL set → POST called. Pins L5682-5701."""
        from custom_components.bosch_shc_camera.const import (
            CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL,
        )
        ev = SimpleNamespace(
            event_type="bosch_shc_camera_motion",
            data={"camera_id": CAM_A, "camera_name": "Terrasse", "timestamp": "2026-05-20T10:00:00Z"},
        )
        session, _ = await self._run_full_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: "https://example.com/hook"},
            ev,
            session_status=200,
        )
        session.post.assert_called_once()
        call = session.post.call_args
        assert call.args[0] == "https://example.com/hook"
        payload = call.kwargs["json"]
        assert payload["event_type"] == "bosch_shc_camera_motion"
        assert payload["camera"] == "Terrasse"

    async def test_deliver_webhook_http_400_logs_warning(self) -> None:
        """_async_deliver_webhook: server returns 400 → warning logged, no exception. Pins L5693-5697."""
        from custom_components.bosch_shc_camera.const import (
            CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL,
        )
        ev = SimpleNamespace(
            event_type="bosch_shc_camera_motion",
            data={"camera_id": CAM_A, "camera_name": "T", "timestamp": ""},
        )
        session, _ = await self._run_full_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: "https://example.com/hook"},
            ev,
            session_status=400,
        )
        # Must not raise and POST must have been called
        session.post.assert_called_once()

    async def test_deliver_webhook_client_error_logged(self) -> None:
        """_async_deliver_webhook: aiohttp.ClientError → caught, not propagated. Pins L5702-5705."""
        import aiohttp
        from custom_components.bosch_shc_camera.const import (
            CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL,
        )

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=aiohttp.ClientConnectionError("timeout"))
        ctx.__aexit__ = AsyncMock(return_value=None)
        error_session = MagicMock()
        error_session.post = MagicMock(return_value=ctx)

        ev = SimpleNamespace(
            event_type="bosch_shc_camera_motion",
            data={"camera_id": CAM_A, "camera_name": "T", "timestamp": ""},
        )
        # Must not raise
        await self._run_full_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: "https://example.com/hook"},
            ev,
            session=error_session,
        )


@pytest.mark.asyncio
class TestSetupEntrySendEventWebhookService:
    """handle_send_event_webhook service handler registered via async_setup_entry."""

    def _make_service_session(self, status: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.post = MagicMock(return_value=ctx)
        return session

    async def _run_service_test(
        self,
        options: dict[str, Any],
        service_call_data: dict[str, Any],
        *,
        session: MagicMock | None = None,
        fake_state: Any = None,
    ) -> tuple[Any, MagicMock]:
        """Run async_setup_entry + invoke service handler, keep patches active throughout."""
        from custom_components.bosch_shc_camera import async_setup_entry

        store_factory = _MultiStore(
            {k: None for k in ["_maint_notified", "_cloud_alert_state", "_lan_ips", "_hw_versions", "_local_creds"]}
        )
        hass = _make_hass()
        if fake_state is not None:
            hass.states.get = MagicMock(return_value=fake_state)
        captured_handler: list[Any] = []

        def _register_service(domain: str, service: str, handler: Any) -> None:
            if service == "send_event_webhook":
                captured_handler.append(handler)

        hass.services.async_register = MagicMock(side_effect=_register_service)
        hass.services.has_service = MagicMock(return_value=False)

        entry = _make_entry(options=options)
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}

        if session is None:
            session = self._make_service_session()

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=_make_ent_reg()), \
             patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_setup_entry(hass, entry)
            handler = captured_handler[0] if captured_handler else None
            call = SimpleNamespace(data=service_call_data)
            if handler:
                await handler(call)

        return hass, session

    async def test_service_handler_registered_when_not_existing(self) -> None:
        """send_event_webhook service registered when has_service returns False. Pins L5750."""
        from custom_components.bosch_shc_camera.const import CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL
        from custom_components.bosch_shc_camera import async_setup_entry

        store_factory = _MultiStore(
            {k: None for k in ["_maint_notified", "_cloud_alert_state", "_lan_ips", "_hw_versions", "_local_creds"]}
        )
        hass = _make_hass()
        hass.services.has_service = MagicMock(return_value=False)
        entry = _make_entry(options={CONF_ENABLE_WEBHOOK_DELIVERY: False, CONF_WEBHOOK_URL: ""})
        coord_stub = _make_coord_stub([CAM_A])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", side_effect=store_factory), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get", return_value=_make_ent_reg()):
            await async_setup_entry(hass, entry)

        calls = [c for c in hass.services.async_register.call_args_list
                 if len(c.args) >= 2 and c.args[1] == "send_event_webhook"]
        assert len(calls) == 1

    async def test_service_handler_disabled_returns_early(self) -> None:
        """handle_send_event_webhook: webhook disabled → no POST. Pins L5717-5718."""
        from custom_components.bosch_shc_camera.const import CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL
        _, session = await self._run_service_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: False, CONF_WEBHOOK_URL: "https://example.com/hook"},
            {"event_type": "MOVEMENT", "entity_id": ""},
        )
        session.post.assert_not_called()

    async def test_service_handler_no_url_returns_early(self) -> None:
        """handle_send_event_webhook: no URL → no POST. Pins L5720-5722."""
        from custom_components.bosch_shc_camera.const import CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL
        _, session = await self._run_service_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: ""},
            {"event_type": "MOVEMENT", "entity_id": ""},
        )
        session.post.assert_not_called()

    async def test_service_handler_posts_with_manual_payload(self) -> None:
        """handle_send_event_webhook: valid options → POST with correct payload. Pins L5733-5746."""
        from custom_components.bosch_shc_camera.const import CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL
        _, session = await self._run_service_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: "https://hook.example.org/test"},
            {"event_type": "PERSON", "entity_id": ""},
        )
        session.post.assert_called_once()
        payload = session.post.call_args.kwargs["json"]
        assert payload["event_type"] == "PERSON"
        assert payload["extra"] == {"source": "manual"}

    async def test_service_handler_resolves_entity_friendly_name(self) -> None:
        """handle_send_event_webhook: entity_id given + state found → camera = friendly_name.
        Pins L5728-5731."""
        from custom_components.bosch_shc_camera.const import CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL
        fake_state = SimpleNamespace(attributes={"friendly_name": "Kamera Terrasse"})
        _, session = await self._run_service_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: "https://hook.example.org/test"},
            {"event_type": "MOVEMENT", "entity_id": "camera.bosch_terrasse"},
            fake_state=fake_state,
        )
        session.post.assert_called_once()
        payload = session.post.call_args.kwargs["json"]
        assert payload["camera"] == "Kamera Terrasse"

    async def test_service_handler_client_error_logged(self) -> None:
        """handle_send_event_webhook: aiohttp.ClientError → caught, no exception. Pins L5746-5747."""
        import aiohttp
        from custom_components.bosch_shc_camera.const import CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=aiohttp.ClientConnectionError("fail"))
        ctx.__aexit__ = AsyncMock(return_value=None)
        error_session = MagicMock()
        error_session.post = MagicMock(return_value=ctx)

        # Must not raise
        await self._run_service_test(
            {CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: "https://example.com/hook"},
            {"event_type": "MOVEMENT", "entity_id": ""},
            session=error_session,
        )
