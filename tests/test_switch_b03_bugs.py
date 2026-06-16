"""Regression tests for B03-switch.md bugs (2026-06-15).

BUG-1: BoschNvrRecordingSwitch.async_turn_on/off never set _nvr_user_intent
        → is_on stays False immediately after toggle.
BUG-2: async_setup_entry guarded by wrong option key "enable_snapshot_button"
        → all switches silently absent when that option is disabled.
BUG-3: BoschAmbientLightSwitch._set_ambient_light does not update
        _ambient_lighting_cache on success → is_on snaps back on next poll.
BUG-4: BoschIntercomSwitch has no RestoreEntity / async_added_to_hass
        → state resets to OFF on every HA restart.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
MODULE = "custom_components.bosch_shc_camera.switch"


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────


def _base_info():
    return {
        "title": "Terrasse",
        "hardwareVersion": "HOME_Eyes_Outdoor",
        "firmwareVersion": "9.40.25",
        "macAddress": "aa:bb:cc:dd:ee:01",
    }


def _stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _bind_hass(sw):
    """Attach a minimal hass so async_write_ha_state doesn't raise."""
    sw.hass = SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    sw.async_write_ha_state = MagicMock()


def _resp_cm(status: int, json_data=None, raise_exc=None):
    """aiohttp-style async context manager mock."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    if raise_exc:
        cm.__aenter__ = AsyncMock(side_effect=raise_exc)
    else:
        cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


# ──────────────────────────────────────────────────────────────────────────────
# BUG-1: NVR switch intent missing
# ──────────────────────────────────────────────────────────────────────────────


def _nvr_coord(**overrides):
    base = dict(
        data={CAM_ID: {"info": _base_info()}},
        _nvr_user_intent={},
        _nvr_processes={},
        _nvr_error_state={},
        _live_connections={},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        start_recorder=AsyncMock(),
        stop_recorder=AsyncMock(),
        options={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestNvrSwitchIntent:
    """BUG-1: is_on reflects intent IMMEDIATELY after toggle (before coord refresh)."""

    def test_is_on_reads_nvr_user_intent(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        assert sw.is_on is False

        coord._nvr_user_intent[CAM_ID] = True
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on_sets_intent_before_write_ha_state(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        await sw.async_turn_on()

        # Intent MUST be set — is_on reads from it
        assert coord._nvr_user_intent[CAM_ID] is True
        coord.start_recorder.assert_awaited_once_with(CAM_ID)
        sw.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_turn_on_is_on_true_immediately(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        assert sw.is_on is False
        await sw.async_turn_on()
        # is_on must be True immediately — no coordinator tick needed
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_intent_false(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord(_nvr_user_intent={CAM_ID: True})
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        assert sw.is_on is True
        await sw.async_turn_off()

        assert coord._nvr_user_intent[CAM_ID] is False
        assert sw.is_on is False
        coord.stop_recorder.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_turn_off_is_on_false_immediately(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord(_nvr_user_intent={CAM_ID: True})
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        await sw.async_turn_off()
        assert sw.is_on is False


# ──────────────────────────────────────────────────────────────────────────────
# BUG-2: Wrong guard key removes all switches
# ──────────────────────────────────────────────────────────────────────────────


def _setup_coord():
    """Minimal coordinator for async_setup_entry tests.

    Must supply every attribute that switch entity __init__ methods touch
    during construction (not async_added_to_hass, which runs later under HA).
    _audio_enabled is seeded by BoschAudioSwitch.__init__ via setdefault().
    """
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {},
                }
            }
        },
        _live_stream_entities={},
        _audio_enabled={},
        options={},
    )


class TestSetupEntryGuard:
    """BUG-2: setting enable_snapshot_button=False must NOT block switches."""

    @pytest.mark.asyncio
    async def test_switches_registered_when_snapshot_button_disabled(self):
        """async_setup_entry must register entities even if enable_snapshot_button=False."""
        from custom_components.bosch_shc_camera.switch import async_setup_entry

        coord = _setup_coord()

        # ConfigEntry has enable_snapshot_button=False — bug: this used to abort early
        entry = SimpleNamespace(
            entry_id="01ENTRY",
            runtime_data=coord,
            data={},
            options={"enable_snapshot_button": False},
        )
        hass = SimpleNamespace()

        added: list = []

        def _add(entities: list, **kw: object) -> None:
            added.extend(entities)

        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=MagicMock(async_get_entity_id=MagicMock(return_value=None)),
        ):
            with patch(
                "custom_components.bosch_shc_camera.switch.get_options",
                return_value={"enable_snapshot_button": False},
            ):
                await async_setup_entry(hass, entry, _add)

        # At least BoschLiveStreamSwitch, BoschAudioSwitch, BoschPrivacyModeSwitch
        # must be present regardless of the snapshot-button option.
        assert len(added) > 0, (
            "No switches registered — the enable_snapshot_button guard incorrectly "
            "blocked all switch entities"
        )

    @pytest.mark.asyncio
    async def test_switches_registered_when_snapshot_button_enabled(self):
        """Baseline: entities are still registered when enable_snapshot_button=True."""
        from custom_components.bosch_shc_camera.switch import async_setup_entry

        coord = _setup_coord()
        entry = SimpleNamespace(
            entry_id="01ENTRY",
            runtime_data=coord,
            data={},
            options={"enable_snapshot_button": True},
        )
        hass = SimpleNamespace()
        added: list = []

        def _add(entities: list, **kw: object) -> None:
            added.extend(entities)

        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=MagicMock(async_get_entity_id=MagicMock(return_value=None)),
        ):
            with patch(
                "custom_components.bosch_shc_camera.switch.get_options",
                return_value={"enable_snapshot_button": True},
            ):
                await async_setup_entry(hass, entry, _add)

        assert len(added) > 0


# ──────────────────────────────────────────────────────────────────────────────
# BUG-3: AmbientLightSwitch cache not updated
# ──────────────────────────────────────────────────────────────────────────────


def _ambient_coord(**overrides):
    base = dict(
        data={CAM_ID: {"info": _base_info()}},
        token="tok-A",
        _ambient_lighting_cache={},
        last_update_success=True,
        is_camera_online=lambda cid: True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestAmbientLightCacheUpdate:
    """BUG-3: _ambient_lighting_cache updated on successful PUT."""

    @pytest.mark.asyncio
    async def test_cache_updated_on_turn_on(self):
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        existing = {"ambientLightEnabled": False, "schedule": "dusk-to-dawn"}
        coord = _ambient_coord(_ambient_lighting_cache={})
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        get_resp = _resp_cm(200, json_data=existing)
        put_resp = _resp_cm(204)
        session = MagicMock()
        session.get = MagicMock(return_value=get_resp)
        session.put = MagicMock(return_value=put_resp)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=session),
        ):
            await sw._set_ambient_light(True)

        # Cache must now contain the updated dict (not empty)
        cache = coord._ambient_lighting_cache.get(CAM_ID)
        assert cache is not None, "Cache not updated after successful PUT"
        assert cache["ambientLightEnabled"] is True
        # is_on should now read True from cache (not _is_on)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_cache_updated_on_turn_off(self):
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        existing = {"ambientLightEnabled": True, "schedule": "dusk-to-dawn"}
        coord = _ambient_coord(_ambient_lighting_cache={})
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        get_resp = _resp_cm(200, json_data=existing)
        put_resp = _resp_cm(200)
        session = MagicMock()
        session.get = MagicMock(return_value=get_resp)
        session.put = MagicMock(return_value=put_resp)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=session),
        ):
            await sw._set_ambient_light(False)

        cache = coord._ambient_lighting_cache.get(CAM_ID)
        assert cache is not None
        assert cache["ambientLightEnabled"] is False
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_cache_not_updated_on_http_error(self):
        """If PUT returns non-2xx, cache must NOT be updated."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        existing = {"ambientLightEnabled": False}
        coord = _ambient_coord(_ambient_lighting_cache={})
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        get_resp = _resp_cm(200, json_data=existing)
        put_resp = _resp_cm(500)  # server error
        session = MagicMock()
        session.get = MagicMock(return_value=get_resp)
        session.put = MagicMock(return_value=put_resp)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=session),
        ):
            await sw._set_ambient_light(True)

        # Cache should remain empty (no update on failure)
        assert coord._ambient_lighting_cache.get(CAM_ID) is None
        assert sw._is_on is None  # not set either

    @pytest.mark.asyncio
    async def test_is_on_prefers_cache_over_is_on_field(self):
        """is_on must return the cache value, not the stale _is_on field."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        # Cache says True, _is_on will be set to True; then simulate a poll that
        # overwrites _is_on (via direct field set) while cache still says True.
        coord = _ambient_coord(
            _ambient_lighting_cache={CAM_ID: {"ambientLightEnabled": True}}
        )
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _stub_entry())
        sw._is_on = False  # stale pre-cache value
        # is_on must prefer the cache
        assert sw.is_on is True


# ──────────────────────────────────────────────────────────────────────────────
# BUG-4: IntercomSwitch missing RestoreEntity
# ──────────────────────────────────────────────────────────────────────────────


class TestIntercomRestoreEntity:
    """BUG-4: IntercomSwitch restores ON/OFF state across HA restarts."""

    def test_intercom_inherits_restore_entity(self):
        from homeassistant.helpers.restore_state import RestoreEntity

        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        assert issubclass(BoschIntercomSwitch, RestoreEntity), (
            "BoschIntercomSwitch must inherit RestoreEntity for state persistence"
        )

    def test_default_is_off_on_first_start(self):
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_restore_on_state_from_previous_session(self):
        """async_added_to_hass must restore ON from last persisted state."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=MagicMock(state="on"))

        # Patch the first base class async_added_to_hass to skip CoordinatorEntity
        # setup (which needs a real coordinator/hass). Pattern from test_switch_sprint_ma.
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_restore_off_state_from_previous_session(self):
        """async_added_to_hass must restore OFF (not flip to default True)."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        sw._is_on = True  # as if turned on during this session
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=MagicMock(state="off"))

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_restore_noop_when_no_previous_state(self):
        """async_added_to_hass with None last_state must leave _is_on False (default)."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=None)

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_restore_noop_for_unknown_state(self):
        """async_added_to_hass must ignore non-on/off states (e.g. 'unavailable')."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=MagicMock(state="unavailable"))

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is False  # unchanged default
