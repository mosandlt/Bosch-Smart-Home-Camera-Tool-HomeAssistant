#!/usr/bin/env python3
"""PARALLEL_UPDATES gate (pr-review-checklist #8): every entity-platform module
must declare a module-level PARALLEL_UPDATES constant — read-only, coordinator-
based platforms should set it to 0. Catches a new platform file forgetting it.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "custom_components/bosch_shc_camera"

PLATFORM_FILES = {
    "switch.py",
    "sensor.py",
    "number.py",
    "select.py",
    "binary_sensor.py",
    "button.py",
    "light.py",
    "update.py",
    "image.py",
    "camera.py",
}

# Matches both `PARALLEL_UPDATES = 0` and the multi-line/parenthesized form
# `PARALLEL_UPDATES = (\n    0  # comment\n)`.
PARALLEL_UPDATES_RE = re.compile(r"^PARALLEL_UPDATES\s*=\s*\(?\s*\d+", re.MULTILINE)


def main() -> int:
    issues: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        if path.name not in PLATFORM_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if not PARALLEL_UPDATES_RE.search(text):
            issues.append(f"  {path.relative_to(ROOT)}: missing PARALLEL_UPDATES")
    if issues:
        print("PARALLEL_UPDATES FAILED — every entity platform module needs it:")
        for issue in issues:
            print(issue)
        return 1
    print(
        f"OK — all {len(PLATFORM_FILES)} entity platform modules declare PARALLEL_UPDATES"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
