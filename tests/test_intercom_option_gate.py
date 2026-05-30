"""Regression test for Fix #5 (2026-05-26): the `enable_intercom` option in
the integration's options flow had no effect — `BoschIntercomSwitch` was
always registered (and only hidden via `_attr_entity_registry_enabled_default
= False`). The user toggle did nothing.

Fix: registration is now gated on `opts.get("enable_intercom", False)` OR a
legacy entity-registry entry (to preserve installs that opted-in via UI).
The `_attr_entity_registry_enabled_default` is dropped so a fresh opt-in
makes the entity immediately visible.

Pin-tests for every mode (PIN_EVERY_MODE).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.switch import (
    BoschIntercomSwitch,
    async_setup_entry,
)

CAM_ID = "DEAD-BEEF-INTERCOM"


def _make_setup_inputs(*, enable_intercom: bool, registry_has_intercom: bool):
    """Build minimal HASS, config_entry, coordinator, async_add_entities."""
    hass = MagicMock()
    config_entry = MagicMock()
    config_entry.options = {
        "enable_intercom": enable_intercom,
        "enable_snapshot_button": True,
    }
    coordinator = MagicMock()
    coordinator.data = {
        CAM_ID: {
            "info": {
                "title": "Testcam",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "firmwareVersion": "9.40.102",
                "macAddress": "AA:BB:CC:00:00:01",
                "featureSupport": {"light": False, "panLimit": 0},
            }
        }
    }
    config_entry.runtime_data = coordinator

    # Fake entity registry: returns an entity_id iff registry has the intercom.
    ent_reg = MagicMock()
    if registry_has_intercom:
        ent_reg.async_get_entity_id.return_value = "switch.testcam_intercom"
    else:
        ent_reg.async_get_entity_id.return_value = None

    added: list = []

    def _async_add_entities(ents, *args, **kwargs):
        added.extend(ents)

    return hass, config_entry, ent_reg, _async_add_entities, added


def _intercom_count(entities) -> int:
    return sum(1 for e in entities if isinstance(e, BoschIntercomSwitch))


class TestIntercomOptionGate:
    @pytest.mark.asyncio
    async def test_option_true_no_legacy_registers(self) -> None:
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=True,
            registry_has_intercom=False,
        )
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        assert _intercom_count(added) == 1

    @pytest.mark.asyncio
    async def test_option_true_with_legacy_registers_once(self) -> None:
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=True,
            registry_has_intercom=True,
        )
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        # Both gates true → still one entity (no duplicate registration).
        assert _intercom_count(added) == 1

    @pytest.mark.asyncio
    async def test_option_false_with_legacy_registers(self) -> None:
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=False,
            registry_has_intercom=True,
        )
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        # Legacy users keep their entity even with option=False.
        assert _intercom_count(added) == 1

    @pytest.mark.asyncio
    async def test_option_false_no_legacy_skips(self) -> None:
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=False,
            registry_has_intercom=False,
        )
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        # Default state: no entity at all.
        assert _intercom_count(added) == 0

    @pytest.mark.asyncio
    async def test_option_missing_default_skips(self) -> None:
        """Option key absent (older install before key was introduced) →
        defaults to False → no entity (unless legacy registry has it)."""
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=False,
            registry_has_intercom=False,
        )
        ce.options = {"enable_snapshot_button": True}  # intercom key missing
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        assert _intercom_count(added) == 0

    def test_class_no_longer_hides_by_default(self) -> None:
        """The `_attr_entity_registry_enabled_default = False` was dropped so
        a fresh opt-in makes the entity immediately visible. If you set this
        back to False, the option toggle becomes confusing (user enables it,
        nothing shows up until they also enable it in the entity registry)."""
        # Check the class's own __dict__ — not inherited attrs from
        # SwitchEntity (which uses a @property descriptor).
        own = BoschIntercomSwitch.__dict__.get("_attr_entity_registry_enabled_default")
        assert own is not False, (
            "BoschIntercomSwitch._attr_entity_registry_enabled_default must not "
            "be set to False on the class — the option toggle now controls visibility."
        )
