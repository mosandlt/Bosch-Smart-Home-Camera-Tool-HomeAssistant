"""Regression test for Bug #4 (2026-05-26): all 3 translation files for the
`use_mjpeg_snapshot` option claimed "On by default" while DEFAULT_OPTIONS in
const.py set it to False. Doc lied for ~6 months, confused users who toggled
it and saw no behavior change.

Fix: doc strings rewritten to match reality (Off by default, experimental).
Pin: strings.json/en.json/de.json must agree with const.DEFAULT_OPTIONS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "bosch_shc_camera"
)


class TestUseMjpegSnapshotDoc:
    def test_default_is_false(self) -> None:
        """Sanity: DEFAULT_OPTIONS still says use_mjpeg_snapshot is OFF.
        If you change this, also update the doc strings."""
        assert DEFAULT_OPTIONS["use_mjpeg_snapshot"] is False

    @pytest.mark.parametrize(
        "rel_path",
        [
            "strings.json",
            "translations/en.json",
        ],
    )
    def test_english_doc_says_off_by_default(self, rel_path: str) -> None:
        text = (COMPONENT / rel_path).read_text(encoding="utf-8")
        data = json.loads(text)
        desc = data["options"]["step"]["init"]["sections"]["stream"][
            "data_description"
        ]["use_mjpeg_snapshot"]
        # Must NOT claim "On by default" (the historical lie).
        assert "on by default" not in desc.lower(), (
            f"{rel_path}: still says 'On by default' — must match DEFAULT_OPTIONS=False"
        )
        # Must explicitly say "Off by default".
        assert "off by default" in desc.lower(), (
            f"{rel_path}: doc must say 'Off by default' to match DEFAULT_OPTIONS"
        )

    def test_german_doc_says_standardmaessig_aus(self) -> None:
        text = (COMPONENT / "translations/de.json").read_text(encoding="utf-8")
        data = json.loads(text)
        desc = data["options"]["step"]["init"]["sections"]["stream"][
            "data_description"
        ]["use_mjpeg_snapshot"]
        assert "standardmäßig aktiviert" not in desc.lower(), (
            "de.json: still says 'Standardmäßig aktiviert' — must say 'Standardmäßig aus'"
        )
        assert "standardmäßig aus" in desc.lower(), (
            "de.json: must say 'Standardmäßig aus' to match DEFAULT_OPTIONS"
        )
