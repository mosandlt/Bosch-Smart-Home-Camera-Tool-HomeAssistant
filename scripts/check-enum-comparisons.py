#!/usr/bin/env python3
"""Enum-identity gate: HA-core enum comparisons must use `is`/`is not`, never `==`/`!=`.

ha-core enforces this via a custom mypy plugin (home-assistant-enum-identity-compare)
because two enum instances backed by different import paths (or a stale cached
member) can be equal-by-value but fail `is`, silently breaking a comparison.
We don't have that plugin available for a custom_component, so this greps for
the pattern directly against every HA-core StrEnum/Enum type this repo imports.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "custom_components/bosch_shc_camera"

# Plain Enum/StrEnum types imported in this repo — NOT IntFlag types
# (CameraEntityFeature, UpdateEntityFeature, etc.), which correctly use `&`.
KNOWN_ENUMS = [
    "EntityCategory",
    "NumberMode",
    "StreamType",
    "CameraState",
    "UpdateDeviceClass",
    "BinarySensorDeviceClass",
    "SensorDeviceClass",
]

PATTERN = re.compile(r"[=!]=\s*(?:" + "|".join(KNOWN_ENUMS) + r")\.")


def main() -> int:
    issues: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PATTERN.search(line):
                issues.append(f"  {path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    if issues:
        print("Enum comparison FAILED — use `is`/`is not`, not `==`/`!=`:")
        for issue in issues:
            print(issue)
        return 1
    print("OK — no `==`/`!=` comparisons against known HA-core enum types")
    return 0


if __name__ == "__main__":
    sys.exit(main())
