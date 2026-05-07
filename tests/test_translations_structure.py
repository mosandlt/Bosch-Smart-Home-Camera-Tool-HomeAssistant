"""Regression tests for translation file structure.

HA frontend resolves options-flow field labels at:
  options.step.init.sections.<section_key>.data.<field>
NOT at the flat options.step.init.data.<field>.

All three files (strings.json, translations/en.json, translations/de.json)
must follow this nested structure or every label shows as a raw underscore key.

Reported by Thomas (session 2026-05-07): ALL toggle labels displayed as raw
Python keys (e.g. enable_snapshots instead of "Camera snapshots") because
the flat data dict was used instead of section-nested data.
"""

import json
from pathlib import Path

import pytest

from custom_components.bosch_shc_camera.config_flow import OPTIONS_SECTIONS

COMP = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"

TRANSLATION_FILES = {
    "strings.json": COMP / "strings.json",
    "en.json": COMP / "translations" / "en.json",
    "de.json": COMP / "translations" / "de.json",
}


def _load(name: str) -> dict:
    return json.loads(TRANSLATION_FILES[name].read_text())


def _init_sections(data: dict) -> dict:
    """Return the options.step.init.sections dict."""
    return (
        data.get("options", {})
        .get("step", {})
        .get("init", {})
        .get("sections", {})
    )


def _all_section_labels(sections: dict) -> set[str]:
    """Collect every field key from all section data blocks."""
    return {k for sec in sections.values() for k in sec.get("data", {})}


class TestTranslationStructure:
    """Verify labels live inside sections, not flat at the step level."""

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_no_flat_data_at_step_level(self, filename):
        """options.step.init must NOT have a top-level 'data' key.

        If labels exist at the flat level HA frontend ignores them and shows
        raw underscore keys instead.
        """
        d = _load(filename)
        init = d.get("options", {}).get("step", {}).get("init", {})
        assert "data" not in init, (
            f"{filename}: found flat 'data' at options.step.init — "
            "labels must be inside sections.<name>.data, not at the step level"
        )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_no_flat_data_description_at_step_level(self, filename):
        """options.step.init must NOT have a top-level 'data_description' key."""
        d = _load(filename)
        init = d.get("options", {}).get("step", {}).get("init", {})
        assert "data_description" not in init, (
            f"{filename}: found flat 'data_description' at options.step.init"
        )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_all_option_fields_have_label_in_sections(self, filename):
        """Every field in OPTIONS_SECTIONS must have a label in the correct section."""
        d = _load(filename)
        sections = _init_sections(d)
        missing = []
        for section_key, fields in OPTIONS_SECTIONS.items():
            section_data = sections.get(section_key, {}).get("data", {})
            for field in fields:
                if field not in section_data:
                    missing.append(f"{section_key}.{field}")
        assert not missing, (
            f"{filename}: missing labels in sections.data: {missing}"
        )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_all_sections_have_name(self, filename):
        """Every section in OPTIONS_SECTIONS must have a translated name."""
        d = _load(filename)
        sections = _init_sections(d)
        for section_key in OPTIONS_SECTIONS:
            assert section_key in sections, (
                f"{filename}: section '{section_key}' missing from sections block"
            )
            assert sections[section_key].get("name"), (
                f"{filename}: section '{section_key}' has no 'name' translation"
            )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_no_extra_fields_in_sections(self, filename):
        """No field in any section.data should be absent from OPTIONS_SECTIONS."""
        d = _load(filename)
        sections = _init_sections(d)
        all_known = {f for fields in OPTIONS_SECTIONS.values() for f in fields}
        for section_key, sec in sections.items():
            for field in sec.get("data", {}):
                assert field in all_known, (
                    f"{filename}: section '{section_key}' has unknown field "
                    f"'{field}' not in OPTIONS_SECTIONS"
                )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_each_field_in_correct_section(self, filename):
        """A field must be in exactly the section OPTIONS_SECTIONS assigns it to."""
        d = _load(filename)
        sections = _init_sections(d)
        for section_key, fields in OPTIONS_SECTIONS.items():
            section_data = sections.get(section_key, {}).get("data", {})
            for field in fields:
                # Also check it isn't duplicated in a wrong section
                wrong = [
                    sk for sk, sec in sections.items()
                    if sk != section_key and field in sec.get("data", {})
                ]
                assert not wrong, (
                    f"{filename}: field '{field}' appears in wrong section(s): {wrong}"
                )

    def test_de_json_uses_german_labels(self):
        """Spot-check a few DE labels to catch copy-paste of EN content."""
        d = _load("de.json")
        sections = _init_sections(d)
        features = sections.get("features", {}).get("data", {})
        # These must be German, not English
        assert features.get("enable_snapshots") != "Camera snapshots", (
            "de.json features.data.enable_snapshots still has English label"
        )
        polling = sections.get("polling", {}).get("data", {})
        assert polling.get("scan_interval") != "Polling interval (seconds)", (
            "de.json polling.data.scan_interval still has English label"
        )
