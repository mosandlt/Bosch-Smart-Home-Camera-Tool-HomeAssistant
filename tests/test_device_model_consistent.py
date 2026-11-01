"""Regression test for GitHub issue #85 ("Camera wrongly recognised").

All entities of one camera share a single device-registry entry, so every
platform's ``device_info["model"]`` must be the same string. select.py and
text.py used to write the raw ``hardwareVersion`` ("HOME_Eyes_Outdoor") while
the other platforms wrote the display name ("Eyes Außenkamera II"); whichever
entity registered last decided what the device showed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "bosch_smart_camera"
PLATFORMS = ["select.py", "text.py"]
RAW_MODEL = re.compile(r'"model":\s*cam_info\.get\("hardwareVersion"')


@pytest.mark.parametrize("filename", PLATFORMS)
def test_device_info_model_uses_display_name(filename: str) -> None:
    """device_info model must go through get_display_name, not the raw API value."""
    source = (COMPONENT / filename).read_text(encoding="utf-8")
    assert not RAW_MODEL.search(source)
    assert "get_display_name(" in source
