"""Regression — entity_id migration for v11.0.0 doubled-prefix bug.

v11.0.0 Gold-Compliance migration set `_attr_has_entity_name = True`
on 30+ entity classes without removing the device-name prefix from
their `_attr_name`. HA prepended the device name a second time and the
buggy entity_id (e.g. `button.bosch_est_bosch_est_refresh_snapshot`)
stuck in the entity_registry for every install that went through that
release.

v12.3.0 fixes the source; this test pins the migration helper that
renames the surviving buggy entries on next setup.

Reported in forum 998974/15 (Andrew75, 2026-05-15).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera import (
    _DOUBLED_PREFIX_RE,
    _migrate_doubled_prefix_entity_ids,
)
from custom_components.bosch_shc_camera.const import DOMAIN


class TestDoubledPrefixRegex:
    """The regex is the load-bearing piece — pin it directly."""

    @pytest.mark.parametrize("eid, expect_domain, expect_slug, expect_rest", [
        ("button.bosch_est_bosch_est_refresh_snapshot", "button", "est", "refresh_snapshot"),
        ("button.bosch_est_bosch_est_siren", "button", "est", "siren"),
        ("update.bosch_est_bosch_est_firmware", "update", "est", "firmware"),
        ("number.bosch_terrasse_bosch_terrasse_pan_position", "number", "terrasse", "pan_position"),
        ("select.bosch_innenbereich_bosch_innenbereich_video_quality",
         "select", "innenbereich", "video_quality"),
        ("light.bosch_garten_bosch_garten_frontlicht", "light", "garten", "frontlicht"),
        ("binary_sensor.bosch_kamera_bosch_kamera_motion", "binary_sensor", "kamera", "motion"),
        # multi-word slug
        ("number.bosch_eyes_outdoor_ii_bosch_eyes_outdoor_ii_lens_elevation",
         "number", "eyes_outdoor_ii", "lens_elevation"),
    ])
    def test_buggy_entity_ids_match(
        self, eid: str, expect_domain: str, expect_slug: str, expect_rest: str
    ):
        m = _DOUBLED_PREFIX_RE.match(eid)
        assert m is not None, f"Should have matched: {eid}"
        assert m.group(1) == expect_domain
        assert m.group(2) == expect_slug
        assert m.group(3) == expect_rest

    @pytest.mark.parametrize("eid", [
        # already-correct entity_ids must NOT match
        "button.bosch_est_refresh_snapshot",
        "number.bosch_terrasse_pan_position",
        "select.bosch_innenbereich_video_quality",
        # camera and switch domains are never buggy — fence them out of the regex
        "camera.bosch_terrasse",
        "switch.bosch_terrasse_live_stream",
        "sensor.bosch_terrasse_fcm_push_status",
        # not our integration at all
        "button.living_room_lamp",
        "switch.bosch_terrasse_bosch_terrasse_live_stream",  # switch not in domain list
        # legitimate doubled token that's NOT a doubled prefix (slug doesn't repeat)
        "button.bosch_est_bosch_west_refresh_snapshot",
    ])
    def test_clean_entity_ids_do_not_match(self, eid: str):
        m = _DOUBLED_PREFIX_RE.match(eid)
        assert m is None, f"Should NOT have matched: {eid}"


class TestMigrateDoubledPrefix:
    """End-to-end migration against a real HA entity_registry."""

    @pytest.fixture
    def config_entry(self, hass) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"bearer_token": "tok", "refresh_token": "rt"},
            unique_id=DOMAIN,
        )
        entry.add_to_hass(hass)
        return entry

    async def test_renames_buggy_entity_ids(self, hass, config_entry):
        """Buggy entries are renamed; non-buggy ones are untouched."""
        ent_reg = er.async_get(hass)
        # 3 buggy entries (button refresh + button siren + update firmware)
        e1 = ent_reg.async_get_or_create(
            "button", DOMAIN, "bosch_shc_refresh_camid1",
            suggested_object_id="bosch_est_bosch_est_refresh_snapshot",
            config_entry=config_entry,
        )
        e2 = ent_reg.async_get_or_create(
            "button", DOMAIN, "bosch_shc_siren_camid1",
            suggested_object_id="bosch_est_bosch_est_siren",
            config_entry=config_entry,
        )
        e3 = ent_reg.async_get_or_create(
            "update", DOMAIN, "bosch_shc_camera_camid1_firmware_update",
            suggested_object_id="bosch_est_bosch_est_firmware",
            config_entry=config_entry,
        )
        # 1 already-correct entry (must NOT be renamed)
        e4 = ent_reg.async_get_or_create(
            "switch", DOMAIN, "bosch_shc_live_stream_camid1",
            suggested_object_id="bosch_est_live_stream",
            config_entry=config_entry,
        )
        # 1 unrelated entity (different config entry, must NOT be touched)
        other_entry = MockConfigEntry(domain="other", data={}, unique_id="other")
        other_entry.add_to_hass(hass)
        e5 = ent_reg.async_get_or_create(
            "button", "other", "external_uid",
            suggested_object_id="bosch_est_bosch_est_refresh_snapshot",
            config_entry=other_entry,
        )

        count = await _migrate_doubled_prefix_entity_ids(hass, config_entry.entry_id)

        assert count == 3
        # buggy got renamed
        assert ent_reg.async_get(e1.entity_id) is None
        assert ent_reg.async_get("button.bosch_est_refresh_snapshot") is not None
        assert ent_reg.async_get(e2.entity_id) is None
        assert ent_reg.async_get("button.bosch_est_siren") is not None
        assert ent_reg.async_get(e3.entity_id) is None
        assert ent_reg.async_get("update.bosch_est_firmware") is not None
        # correct one untouched
        assert ent_reg.async_get(e4.entity_id) is not None
        # other-integration entity untouched
        assert ent_reg.async_get(e5.entity_id) is not None

    async def test_creates_repair_issue_when_renames_happen(self, hass, config_entry):
        ent_reg = er.async_get(hass)
        ent_reg.async_get_or_create(
            "button", DOMAIN, "uid1",
            suggested_object_id="bosch_est_bosch_est_refresh_snapshot",
            config_entry=config_entry,
        )
        ent_reg.async_get_or_create(
            "number", DOMAIN, "uid2",
            suggested_object_id="bosch_est_bosch_est_pan_position",
            config_entry=config_entry,
        )

        issue_reg = ir.async_get(hass)
        await _migrate_doubled_prefix_entity_ids(hass, config_entry.entry_id)
        issue = issue_reg.async_get_issue(DOMAIN, "doubled_prefix_entity_ids_migrated")
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.is_fixable is False
        assert issue.translation_placeholders["count"] == "2"

    async def test_no_issue_when_no_buggy_entries(self, hass, config_entry):
        ent_reg = er.async_get(hass)
        ent_reg.async_get_or_create(
            "switch", DOMAIN, "uid1",
            suggested_object_id="bosch_est_live_stream",
            config_entry=config_entry,
        )
        issue_reg = ir.async_get(hass)
        # Pre-seed the issue to verify it gets deleted on a clean run
        ir.async_create_issue(
            hass, DOMAIN, "doubled_prefix_entity_ids_migrated",
            is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="doubled_prefix_entity_ids_migrated",
        )
        assert issue_reg.async_get_issue(DOMAIN, "doubled_prefix_entity_ids_migrated") is not None

        count = await _migrate_doubled_prefix_entity_ids(hass, config_entry.entry_id)
        assert count == 0
        assert issue_reg.async_get_issue(DOMAIN, "doubled_prefix_entity_ids_migrated") is None

    async def test_skips_when_new_entity_id_already_exists(self, hass, config_entry):
        """Defensive: if both buggy and correct entries somehow coexist,
        skip rather than raise on the unique-entity_id constraint."""
        ent_reg = er.async_get(hass)
        ent_reg.async_get_or_create(
            "button", DOMAIN, "uid_old",
            suggested_object_id="bosch_est_bosch_est_refresh_snapshot",
            config_entry=config_entry,
        )
        ent_reg.async_get_or_create(
            "button", DOMAIN, "uid_new",
            suggested_object_id="bosch_est_refresh_snapshot",
            config_entry=config_entry,
        )

        count = await _migrate_doubled_prefix_entity_ids(hass, config_entry.entry_id)
        assert count == 0
        # both still exist
        assert ent_reg.async_get("button.bosch_est_bosch_est_refresh_snapshot") is not None
        assert ent_reg.async_get("button.bosch_est_refresh_snapshot") is not None
