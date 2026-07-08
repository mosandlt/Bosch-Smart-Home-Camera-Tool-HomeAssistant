#!/usr/bin/env python3
"""Docstring hygiene gate, from real ha-core PR review lessons (pr-review-checklist #3).

Checks:
  - No doubled trailing periods at the end of a docstring.
  - A class's own docstring must not name a *different* entity-platform class kind
    than the one it's actually on (e.g. a switch's docstring starting "Sensor:" —
    a copy-paste leftover from an earlier class in the same file).
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "custom_components/bosch_shc_camera"

# Exactly two periods (not three+, e.g. a code sample's "...") at the end of a line.
DOUBLE_PERIOD = re.compile(r"(?<!\.)\.\.(?!\.)\s*$")

# Entity "kind" words we expect a class docstring to open with, keyed by the
# platform module the class lives in — catches "Switch:"-on-a-Sensor-class
# copy-paste leftovers. Only checked for classes whose docstring opens with
# "<Kind>:" at all; classes that don't follow this convention are skipped.
KIND_BY_MODULE = {
    "switch.py": "Switch",
    "sensor.py": "Sensor",
    "number.py": "Number",
    "select.py": "Select",
    "binary_sensor.py": "Binary sensor",
    "button.py": "Button",
    "light.py": "Light",
    "update.py": "Update",
    "image.py": "Image",
    "camera.py": "Camera",
}
OTHER_KINDS = set(KIND_BY_MODULE.values())


def main() -> int:
    issues: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        expected_kind = KIND_BY_MODULE.get(path.name)
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            doc = ast.get_docstring(node, clean=False)
            if not doc:
                continue
            lineno = node.lineno
            if DOUBLE_PERIOD.search(
                doc.rstrip().splitlines()[-1] if doc.strip() else ""
            ):
                issues.append(
                    f"  {path.relative_to(ROOT)}:{lineno}: {node.name} — doubled trailing period"
                )
            if isinstance(node, ast.ClassDef) and expected_kind:
                first_line = doc.strip().splitlines()[0] if doc.strip() else ""
                m = re.match(r"([A-Za-z ]+):", first_line)
                if m and m.group(1) in OTHER_KINDS and m.group(1) != expected_kind:
                    issues.append(
                        f"  {path.relative_to(ROOT)}:{lineno}: class {node.name} "
                        f"docstring says '{m.group(1)}:' but this is a {expected_kind} platform "
                        f"file — likely copy-paste leftover"
                    )
    if issues:
        print("Docstring hygiene FAILED:")
        for issue in issues:
            print(issue)
        return 1
    print("OK — no doubled trailing periods or mismatched entity-kind docstrings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
