"""Coverage for AI describe-snapshot closures in async_setup_entry.

Targets in __init__.py:
  8016-8017   _async_deliver_webhook: invalid URL scheme → rejected
  8053-8233   handle_describe_snapshot: all branches
  8241-8273   _async_auto_describe: all branches
  8312-8316   handle_send_event_webhook: invalid URL scheme → rejected
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"
CAM_ID = "11111111-1111-1111-1111-111111111111"
DOMAIN = "bosch_shc_camera"


# ─────────────────────────────────────────────────────────────────────────────
# Shared test helpers
# ─────────────────────────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def async_load(self) -> Any:
        return self._payload

    async def async_save(self, data: Any) -> None:
        pass


class _MultiStore:
    def __init__(self, payloads: dict[str, Any]) -> None:
        self._payloads = payloads

    def __call__(self, hass: Any, *, version: int, key: str) -> _FakeStore:
        for suffix, payload in self._payloads.items():
            if key.endswith(suffix):
                return _FakeStore(payload)
        return _FakeStore(None)


def _make_coord_stub(camera_ids: list[str], entry: Any = None) -> MagicMock:
    coord = MagicMock()
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
    coord.async_load_ai_budget = AsyncMock(return_value=None)
    coord._shc_state_cache = {}
    coord._camera_entities = {CAM_ID: SimpleNamespace(entity_id="camera.bosch_test")}
    coord._ai_in_flight = 0
    coord._ai_record_call = MagicMock()
    coord.async_generate_ai_description = AsyncMock()
    coord.async_set_updated_data = MagicMock()
    if entry is not None:
        coord._entry = entry
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
    hass.config_entries.flow.async_init = AsyncMock(
        return_value={"type": "create_entry"}
    )
    hass.async_create_task = MagicMock()
    hass.async_create_background_task = MagicMock()
    hass.bus.async_listen_once = MagicMock(return_value=lambda: None)
    hass.bus.async_listen = MagicMock(return_value=lambda: None)
    hass.bus.async_fire = MagicMock()
    hass.services.has_service = MagicMock(return_value=False)
    hass.services.async_register = MagicMock()
    hass.services.async_call = AsyncMock(return_value={"data": "A person is visible."})
    hass.states = MagicMock()
    hass.loop.time = MagicMock(return_value=1000.0)
    return hass


def _make_ent_reg() -> MagicMock:
    ent_reg = MagicMock()
    ent_reg.async_get_entity_id = MagicMock(return_value=None)
    return ent_reg


def _make_store_factory() -> _MultiStore:
    keys = [
        "_maint_notified",
        "_cloud_alert_state",
        "_lan_ips",
        "_hw_versions",
        "_local_creds",
    ]
    return _MultiStore({k: None for k in keys})


async def _setup_and_get_handlers(
    hass: MagicMock,
    entry: MagicMock,
    coord_stub: MagicMock,
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Run async_setup_entry, capture service handlers and bus listeners."""
    from custom_components.bosch_shc_camera import async_setup_entry

    store_factory = _make_store_factory()
    registered_handlers: dict[str, Any] = {}
    bus_listeners: dict[str, list[Any]] = {}

    def _on_register(domain: str, service: str, handler: Any, **kwargs: Any) -> None:
        registered_handlers[service] = handler

    def _on_listen(event_type: str, handler: Any) -> Any:
        bus_listeners.setdefault(event_type, []).append(handler)
        return lambda: None

    hass.services.async_register = MagicMock(side_effect=_on_register)
    hass.bus.async_listen = MagicMock(side_effect=_on_listen)

    with (
        patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub),
        patch("homeassistant.helpers.storage.Store", side_effect=store_factory),
        patch(f"{MODULE}.cf_unbuffer.register"),
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=_make_ent_reg(),
        ),
        patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()),
    ):
        await async_setup_entry(hass, entry)

    return registered_handlers, bus_listeners


def _make_call(data: dict[str, Any]) -> MagicMock:
    call = MagicMock()
    call.data = data
    return call


def _make_event(data: dict[str, Any], event_type: str = "test") -> MagicMock:
    evt = MagicMock()
    evt.data = data
    evt.event_type = event_type
    return evt


# ─────────────────────────────────────────────────────────────────────────────
# _async_deliver_webhook  (lines 8016-8017)
# ─────────────────────────────────────────────────────────────────────────────


class TestDeliverWebhookInvalidScheme:
    """_async_deliver_webhook rejects non-http(s) URLs (lines 8016-8017)."""

    @pytest.mark.asyncio
    async def test_ftp_scheme_rejected(self) -> None:
        """ftp:// URL → warning + return without POST."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": True,
                "webhook_url": "ftp://bad.example.com/hook",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        # Grab any of the four WEBHOOK_EVENT_TYPES listeners
        deliver_fn = listeners.get("bosch_shc_camera_motion", [None])[0]
        assert deliver_fn is not None

        session_mock = MagicMock()
        session_mock.post = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await deliver_fn(_make_event({}, "bosch_shc_camera_motion"))

        # POST must NOT be called when URL scheme is invalid
        session_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_scheme_rejected(self) -> None:
        """file:// URL → warning + return without POST."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": True,
                "webhook_url": "file:///etc/passwd",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        deliver_fn = listeners.get("bosch_shc_camera_audio_alarm", [None])[0]
        assert deliver_fn is not None

        session_mock = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await deliver_fn(_make_event({}, "bosch_shc_camera_audio_alarm"))

        session_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_https_scheme_allowed(self) -> None:
        """https:// URL → proceeds to POST (lines 8016-8017 NOT hit)."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": True,
                "webhook_url": "https://good.example.com/hook",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        deliver_fn = listeners.get("bosch_shc_camera_motion", [None])[0]
        assert deliver_fn is not None

        resp_mock = AsyncMock()
        resp_mock.status = 200
        cm_mock = AsyncMock()
        cm_mock.__aenter__ = AsyncMock(return_value=resp_mock)
        cm_mock.__aexit__ = AsyncMock(return_value=False)
        session_mock = MagicMock()
        session_mock.post = MagicMock(return_value=cm_mock)
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await deliver_fn(
                _make_event({"camera_id": CAM_ID}, "bosch_shc_camera_motion")
            )

        session_mock.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_disabled_short_circuits(self) -> None:
        """enable_webhook_delivery=False → returns before URL check."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": False,
                "webhook_url": "ftp://bad.example.com/hook",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        deliver_fn = listeners.get("bosch_shc_camera_motion", [None])[0]
        assert deliver_fn is not None

        session_mock = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await deliver_fn(_make_event({}, "bosch_shc_camera_motion"))

        session_mock.post.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# handle_describe_snapshot  (lines 8053-8233)
# ─────────────────────────────────────────────────────────────────────────────


class TestDescribeSnapshotArgumentRequired:
    """Lines 8060-8065: no camera_id AND no entity_id → ServiceValidationError."""

    @pytest.mark.asyncio
    async def test_no_ids_raises(self) -> None:
        from homeassistant.exceptions import ServiceValidationError

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        with pytest.raises(ServiceValidationError):
            await handler(_make_call({"camera_id": "", "entity_id": ""}))


class TestDescribeSnapshotNoLoadedEntries:
    """Lines 8067-8076: no loaded entries → HomeAssistantError."""

    @pytest.mark.asyncio
    async def test_no_entries_raises(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[])

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        with pytest.raises(HomeAssistantError):
            await handler(_make_call({"camera_id": CAM_ID}))


class TestDescribeSnapshotCameraIdPath:
    """Lines 8083-8092: camera_id path — coord found via _camera_entities lookup."""

    @pytest.mark.asyncio
    async def test_camera_id_resolves(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "Motion detected."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        result = await handler(_make_call({"camera_id": CAM_ID}))
        assert result["description"] == "Motion detected."

    @pytest.mark.asyncio
    async def test_camera_id_writes_ai_description(self) -> None:
        """Lines 8217-8223: cam_id in coord.data → ai_description written."""
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "Patrol van."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        await handler(_make_call({"camera_id": CAM_ID}))

        assert "ai_description" in coord_stub.data[CAM_ID]
        assert coord_stub.data[CAM_ID]["ai_description"]["text"] == "Patrol van."
        coord_stub.async_set_updated_data.assert_called_once()


class TestDescribeSnapshotEntityIdPath:
    """Lines 8093-8102: entity_id path — coord found by iterating _camera_entities."""

    @pytest.mark.asyncio
    async def test_entity_id_resolves(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "No observations."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        result = await handler(_make_call({"entity_id": "camera.bosch_test"}))
        assert result["description"] == "No observations."


class TestDescribeSnapshotFallbackAndNoCoord:
    """Lines 8103-8118: fallback + no active coordinator."""

    @pytest.mark.asyncio
    async def test_fallback_to_first_coord(self) -> None:
        """Lines 8103-8109: cam_id unknown → fallback to first entry's coord."""
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        # Clear camera_entities so cam lookup fails → triggers fallback
        coord_stub._camera_entities = {}
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "description here"})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        # camera_id "unknown" → not found in _camera_entities → fallback used
        # resolved_entity_id stays "" → ServiceValidationError(not_found)
        from homeassistant.exceptions import ServiceValidationError

        with pytest.raises(ServiceValidationError):
            await handler(_make_call({"camera_id": "unknown-id"}))

    @pytest.mark.asyncio
    async def test_no_active_coordinator_raises(self) -> None:
        """Lines 8110-8118: runtime_data=None on all entries → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError

        hass = _make_hass()
        entry = _make_entry()
        entry.runtime_data = None
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        entry2 = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry2)
        entry2.runtime_data = coord_stub

        handlers, _ = await _setup_and_get_handlers(hass, entry2, coord_stub)
        handler = handlers["describe_snapshot"]

        # All entries have runtime_data=None
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        with pytest.raises(HomeAssistantError):
            await handler(_make_call({"camera_id": CAM_ID}))


class TestDescribeSnapshotPrivacyGuard:
    """Lines 8121-8127: privacy mode active → ServiceValidationError(privacy_active)."""

    @pytest.mark.asyncio
    async def test_privacy_active_raises(self) -> None:
        from homeassistant.exceptions import ServiceValidationError

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        coord_stub._shc_state_cache = {CAM_ID: {"privacy_mode": True}}
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_make_call({"camera_id": CAM_ID}))
        assert "privacy_active" in str(exc_info.value.translation_key)

    @pytest.mark.asyncio
    async def test_privacy_inactive_proceeds(self) -> None:
        """privacy_mode=False → no exception."""
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        coord_stub._shc_state_cache = {CAM_ID: {"privacy_mode": False}}
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "Person in frame."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        result = await handler(_make_call({"camera_id": CAM_ID}))
        assert result["description"] == "Person in frame."


class TestDescribeSnapshotEntityNotFound:
    """Lines 8129-8137: resolved_entity_id empty → ServiceValidationError(not_found)."""

    @pytest.mark.asyncio
    async def test_entity_not_found_raises(self) -> None:
        from homeassistant.exceptions import ServiceValidationError

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        # entity_id lookup via entity_id_arg but no match
        coord_stub._camera_entities = {}  # no cameras registered
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_make_call({"entity_id": "camera.nonexistent"}))
        assert "not_found" in str(exc_info.value.translation_key)


class TestDescribeSnapshotAiTaskEntitySet:
    """Lines 8174-8175: ai_task_entity_used → ai_call_data["entity_id"] set."""

    @pytest.mark.asyncio
    async def test_ai_task_entity_passed(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "Car parked."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        result = await handler(
            _make_call(
                {"camera_id": CAM_ID, "ai_task_entity": "conversation.assistant_1"}
            )
        )
        assert result["description"] == "Car parked."
        # Verify entity_id was passed to the ai_task service call
        call_kwargs = hass.services.async_call.call_args
        assert call_kwargs[0][2]["entity_id"] == "conversation.assistant_1"

    @pytest.mark.asyncio
    async def test_ai_task_entity_from_options(self) -> None:
        """ai_task_entity from entry options (not call data)."""
        hass = _make_hass()
        entry = _make_entry(options={"ai_task_entity": "conversation.opt_entity"})
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "Bicycle."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        await handler(_make_call({"camera_id": CAM_ID}))
        call_kwargs = hass.services.async_call.call_args
        assert call_kwargs[0][2]["entity_id"] == "conversation.opt_entity"


class TestDescribeSnapshotInFlightTracking:
    """Lines 8182-8207: _ai_in_flight increment/decrement."""

    @pytest.mark.asyncio
    async def test_in_flight_incremented_and_decremented(self) -> None:
        """Lines 8182-8183 + 8206-8207: counter goes up then back down."""
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "All clear."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        initial = coord_stub._ai_in_flight
        await handler(_make_call({"camera_id": CAM_ID}))
        assert coord_stub._ai_in_flight == initial  # decremented back

    @pytest.mark.asyncio
    async def test_in_flight_decremented_on_timeout(self) -> None:
        """Lines 8193-8207: TimeoutError → HomeAssistantError, counter decremented."""
        from homeassistant.exceptions import HomeAssistantError

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(side_effect=TimeoutError("timed out"))

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        initial = coord_stub._ai_in_flight
        with pytest.raises(HomeAssistantError) as exc_info:
            await handler(_make_call({"camera_id": CAM_ID}))
        assert "ai_task_unavailable" in str(exc_info.value.translation_key)
        assert coord_stub._ai_in_flight == initial  # still decremented

    @pytest.mark.asyncio
    async def test_in_flight_decremented_on_generic_exception(self) -> None:
        """Lines 8199-8207: generic Exception → HomeAssistantError, counter decremented."""
        from homeassistant.exceptions import HomeAssistantError

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(
            side_effect=RuntimeError("service unavailable")
        )

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        initial = coord_stub._ai_in_flight
        with pytest.raises(HomeAssistantError) as exc_info:
            await handler(_make_call({"camera_id": CAM_ID}))
        assert "ai_task_unavailable" in str(exc_info.value.translation_key)
        assert coord_stub._ai_in_flight == initial


class TestDescribeSnapshotEmptyText:
    """Lines 8212-8213: empty response text → return {"description": ""}."""

    @pytest.mark.asyncio
    async def test_empty_data_returns_empty_description(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": ""})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        result = await handler(_make_call({"camera_id": CAM_ID}))
        assert result == {"description": ""}
        # _ai_record_call NOT called for empty text
        coord_stub._ai_record_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_empty_description(self) -> None:
        """Whitespace-only response stripped → empty → return {"description": ""}."""
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "   "})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        result = await handler(_make_call({"camera_id": CAM_ID}))
        assert result == {"description": ""}


class TestDescribeSnapshotBusEvent:
    """Lines 8224-8233: bus event fired + return {"description": text}."""

    @pytest.mark.asyncio
    async def test_bus_event_fired(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "Dog in yard."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        result = await handler(_make_call({"camera_id": CAM_ID}))
        assert result["description"] == "Dog in yard."
        hass.bus.async_fire.assert_called_once()
        fired_event, fired_data = hass.bus.async_fire.call_args[0]
        assert fired_event == "bosch_shc_camera_ai_description"
        assert fired_data["camera_id"] == CAM_ID
        assert fired_data["description"] == "Dog in yard."

    @pytest.mark.asyncio
    async def test_ai_record_call_invoked(self) -> None:
        """Lines 8214-8215: _ai_record_call called with cam_id."""
        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.services.async_call = AsyncMock(return_value={"data": "Package dropped."})

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        handler = handlers["describe_snapshot"]

        await handler(_make_call({"camera_id": CAM_ID}))
        coord_stub._ai_record_call.assert_called_once_with(CAM_ID)


# ─────────────────────────────────────────────────────────────────────────────
# _async_auto_describe  (lines 8241-8273)
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoDescribeDebounce:
    """Lines 8244-8245: debounce hit → return early."""

    @pytest.mark.asyncio
    async def test_debounce_prevents_call(self) -> None:
        import custom_components.bosch_shc_camera as _init_mod

        hass = _make_hass()
        entry = _make_entry(options={"ai_describe_on_motion": True})
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        # Inject a recent debounce timestamp (now - 5s, well within 30s window)
        _init_mod._AI_MOTION_DEBOUNCE[CAM_ID] = 995.0  # hass.loop.time() = 1000.0
        try:
            auto_fn = listeners.get("bosch_shc_camera_motion", [None])[-1]
            assert auto_fn is not None
            await auto_fn(_make_event({"camera_id": CAM_ID}))
            coord_stub.async_generate_ai_description.assert_not_called()
        finally:
            _init_mod._AI_MOTION_DEBOUNCE.clear()

    @pytest.mark.asyncio
    async def test_no_debounce_proceeds(self) -> None:
        """Stale debounce (float('-inf')) → proceeds."""
        import custom_components.bosch_shc_camera as _init_mod

        hass = _make_hass()
        entry = _make_entry(options={"ai_describe_on_motion": True})
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        _init_mod._AI_MOTION_DEBOUNCE.clear()
        try:
            auto_fn = listeners.get("bosch_shc_camera_motion", [None])[-1]
            assert auto_fn is not None
            await auto_fn(_make_event({"camera_id": CAM_ID}))
            coord_stub.async_generate_ai_description.assert_called_once_with(
                CAM_ID, force=False
            )
        finally:
            _init_mod._AI_MOTION_DEBOUNCE.clear()


class TestAutoDescribeNoLoadedEntries:
    """Lines 8247-8248: no loaded entries → return early."""

    @pytest.mark.asyncio
    async def test_no_entries_returns_early(self) -> None:
        import custom_components.bosch_shc_camera as _init_mod

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        # After setup, override loaded_entries to return empty
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[])

        _init_mod._AI_MOTION_DEBOUNCE.clear()
        try:
            auto_fn = listeners.get("bosch_shc_camera_motion", [None])[-1]
            assert auto_fn is not None
            await auto_fn(_make_event({"camera_id": CAM_ID}))
            coord_stub.async_generate_ai_description.assert_not_called()
        finally:
            _init_mod._AI_MOTION_DEBOUNCE.clear()


class TestAutoDescribeNoEntityFound:
    """Lines 8260-8262: no entity found for cam_id → return (logs debug)."""

    @pytest.mark.asyncio
    async def test_unknown_cam_id_returns_early(self) -> None:
        import custom_components.bosch_shc_camera as _init_mod

        hass = _make_hass()
        entry = _make_entry(options={"ai_describe_on_motion": True})
        coord_stub = _make_coord_stub([CAM_ID], entry)
        coord_stub._camera_entities = {}  # no cameras → entity not found
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        _init_mod._AI_MOTION_DEBOUNCE.clear()
        try:
            auto_fn = listeners.get("bosch_shc_camera_motion", [None])[-1]
            assert auto_fn is not None
            await auto_fn(_make_event({"camera_id": "unknown-cam"}))
            coord_stub.async_generate_ai_description.assert_not_called()
        finally:
            _init_mod._AI_MOTION_DEBOUNCE.clear()


class TestAutoDescribeOptionDisabled:
    """Lines 8264-8265: ai_describe_on_motion=False → return."""

    @pytest.mark.asyncio
    async def test_option_disabled_returns_early(self) -> None:
        import custom_components.bosch_shc_camera as _init_mod

        hass = _make_hass()
        entry = _make_entry(options={"ai_describe_on_motion": False})
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        _init_mod._AI_MOTION_DEBOUNCE.clear()
        try:
            auto_fn = listeners.get("bosch_shc_camera_motion", [None])[-1]
            assert auto_fn is not None
            await auto_fn(_make_event({"camera_id": CAM_ID}))
            coord_stub.async_generate_ai_description.assert_not_called()
        finally:
            _init_mod._AI_MOTION_DEBOUNCE.clear()

    @pytest.mark.asyncio
    async def test_option_missing_returns_early(self) -> None:
        """ai_describe_on_motion key absent (defaults False) → return."""
        import custom_components.bosch_shc_camera as _init_mod

        hass = _make_hass()
        entry = _make_entry(options={})
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        _init_mod._AI_MOTION_DEBOUNCE.clear()
        try:
            auto_fn = listeners.get("bosch_shc_camera_motion", [None])[-1]
            assert auto_fn is not None
            await auto_fn(_make_event({"camera_id": CAM_ID}))
            coord_stub.async_generate_ai_description.assert_not_called()
        finally:
            _init_mod._AI_MOTION_DEBOUNCE.clear()


class TestAutoDescribeDebounceTimestampUpdated:
    """Lines 8269+8271: debounce timestamp written + async_generate_ai_description called."""

    @pytest.mark.asyncio
    async def test_debounce_written_and_call_made(self) -> None:
        import custom_components.bosch_shc_camera as _init_mod

        hass = _make_hass()
        entry = _make_entry(options={"ai_describe_on_motion": True})
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.loop.time = MagicMock(return_value=5000.0)

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        _init_mod._AI_MOTION_DEBOUNCE.clear()
        try:
            auto_fn = listeners.get("bosch_shc_camera_motion", [None])[-1]
            assert auto_fn is not None
            await auto_fn(_make_event({"camera_id": CAM_ID}))
            assert _init_mod._AI_MOTION_DEBOUNCE.get(CAM_ID) == 5000.0
            coord_stub.async_generate_ai_description.assert_called_once_with(
                CAM_ID, force=False
            )
        finally:
            _init_mod._AI_MOTION_DEBOUNCE.clear()


class TestAutoDescribeExceptionCaught:
    """Lines 8272-8273: exception in async_generate_ai_description → caught, logged debug."""

    @pytest.mark.asyncio
    async def test_exception_is_swallowed(self) -> None:
        import custom_components.bosch_shc_camera as _init_mod

        hass = _make_hass()
        entry = _make_entry(options={"ai_describe_on_motion": True})
        coord_stub = _make_coord_stub([CAM_ID], entry)
        coord_stub.async_generate_ai_description = AsyncMock(
            side_effect=RuntimeError("AI offline")
        )
        entry.runtime_data = coord_stub
        coord_stub._entry = entry
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        _handlers, listeners = await _setup_and_get_handlers(hass, entry, coord_stub)

        _init_mod._AI_MOTION_DEBOUNCE.clear()
        try:
            auto_fn = listeners.get("bosch_shc_camera_motion", [None])[-1]
            assert auto_fn is not None
            # Should NOT raise — exception must be swallowed
            await auto_fn(_make_event({"camera_id": CAM_ID}))
        finally:
            _init_mod._AI_MOTION_DEBOUNCE.clear()


# ─────────────────────────────────────────────────────────────────────────────
# handle_send_event_webhook invalid scheme  (lines 8312-8316)
# ─────────────────────────────────────────────────────────────────────────────


class TestSendEventWebhookInvalidScheme:
    """Lines 8312-8316: handle_send_event_webhook rejects non-http(s) URLs."""

    @pytest.mark.asyncio
    async def test_ftp_scheme_rejected(self) -> None:
        """ftp:// URL → warning + return without POST."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": True,
                "webhook_url": "ftp://bad.example.com/hook",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        handler = handlers.get("send_event_webhook")
        assert handler is not None

        session_mock = MagicMock()
        session_mock.post = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await handler(_make_call({"event_type": "MOVEMENT"}))

        session_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_gopher_scheme_rejected(self) -> None:
        """gopher:// URL → warning + return without POST."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": True,
                "webhook_url": "gopher://ancient.example.com/",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        handler = handlers.get("send_event_webhook")
        assert handler is not None

        session_mock = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await handler(_make_call({"event_type": "INTRUSION"}))

        session_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_scheme_allowed(self) -> None:
        """http:// URL → proceeds past invalid-scheme check (lines 8312-8316 NOT hit)."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": True,
                "webhook_url": "http://good.example.com/hook",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        handler = handlers.get("send_event_webhook")
        assert handler is not None

        resp_mock = AsyncMock()
        resp_mock.status = 200
        cm_mock = AsyncMock()
        cm_mock.__aenter__ = AsyncMock(return_value=resp_mock)
        cm_mock.__aexit__ = AsyncMock(return_value=False)
        session_mock = MagicMock()
        session_mock.post = MagicMock(return_value=cm_mock)
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await handler(_make_call({"event_type": "AUDIO_ALARM"}))

        session_mock.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_webhook_no_loaded_entries(self) -> None:
        """Lines 8295-8300: no loaded entries → warning + return."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": True,
                "webhook_url": "http://example.com/hook",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[])

        handler = handlers.get("send_event_webhook")
        assert handler is not None

        session_mock = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await handler(_make_call({"event_type": "MOVEMENT"}))

        session_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_webhook_delivery_disabled(self) -> None:
        """Lines 8302-8306: enable_webhook_delivery=False → return."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": False,
                "webhook_url": "http://example.com/hook",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        handler = handlers.get("send_event_webhook")
        assert handler is not None

        session_mock = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await handler(_make_call({"event_type": "MOVEMENT"}))

        session_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_webhook_empty_url(self) -> None:
        """Lines 8307-8310: empty URL → return."""
        hass = _make_hass()
        entry = _make_entry(
            options={
                "enable_webhook_delivery": True,
                "webhook_url": "",
            }
        )
        coord_stub = _make_coord_stub([CAM_ID], entry)
        entry.runtime_data = coord_stub

        handlers, _ = await _setup_and_get_handlers(hass, entry, coord_stub)
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        handler = handlers.get("send_event_webhook")
        assert handler is not None

        session_mock = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session_mock):
            await handler(_make_call({"event_type": "MOVEMENT"}))

        session_mock.post.assert_not_called()
