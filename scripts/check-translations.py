#!/usr/bin/env python3
"""Translation completeness gate: every key in strings.json must exist in every translations/*.json."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
STRINGS = ROOT / "custom_components/bosch_shc_camera/strings.json"
TRANSLATIONS = ROOT / "custom_components/bosch_shc_camera/translations"


def flatten(obj: object, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif not isinstance(obj, list):
        out[prefix] = str(obj)
    return out


def main() -> int:
    ref = json.loads(STRINGS.read_text(encoding="utf-8"))
    issues: list[str] = []
    for path in sorted(TRANSLATIONS.glob("*.json")):
        lang = path.stem
        tr = json.loads(path.read_text(encoding="utf-8"))
        for section in ("entity", "config", "options"):
            ref_flat = flatten(ref.get(section, {}))
            tr_flat = flatten(tr.get(section, {}))
            missing = [k for k in ref_flat if k not in tr_flat]
            extra = [k for k in tr_flat if k not in ref_flat]
            if missing:
                issues.append(
                    f"  {lang} [{section}]: missing {len(missing)} key(s): {missing[:5]}"
                )
            if extra:
                issues.append(
                    f"  {lang} [{section}]: extra {len(extra)} key(s): {extra[:5]}"
                )
    if issues:
        print("Translation completeness FAILED:")
        for issue in issues:
            print(issue)
        return 1
    print(
        f"OK — all {len(list(TRANSLATIONS.glob('*.json')))} translation files complete"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
