#!/usr/bin/env python3
"""Entity-name sentence-case gate (pr-review-checklist #12, ha-core convention).

English source (strings.json) entity names must be sentence case: only the
first word capitalized, plus a fixed allowlist of acronyms/proper nouns that
are always capitalized regardless of position (LED, WiFi, NVR, RCP, TLS, IVA,
ONVIF, FCM, AI, URL, RTSP, LAN, Bosch, Frigate).

Only strings.json is checked — translations/*.json follow each language's own
capitalization rules (e.g. German capitalizes all nouns), not English sentence
case, so they are intentionally NOT held to this rule.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
STRINGS = ROOT / "custom_components/bosch_shc_camera/strings.json"

ALWAYS_CAPS = {
    "LED",
    "WiFi",
    "NVR",
    "RCP",
    "TLS",
    "IVA",
    "ONVIF",
    "FCM",
    "AI",
    "URL",
    "RTSP",
    "LAN",
    "Bosch",
    "Frigate",
    "Mini-NVR",
    "HA",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def is_sentence_case(name: str) -> bool:
    words = WORD_RE.findall(name)
    if not words:
        return True
    for i, word in enumerate(words):
        if word in ALWAYS_CAPS:
            continue
        if i == 0:
            if not word[0].isupper():
                return False
        else:
            if word[0].isupper() and word.lower() not in {
                w.lower() for w in ALWAYS_CAPS
            }:
                return False
    return True


def main() -> int:
    ref = json.loads(STRINGS.read_text(encoding="utf-8"))
    issues: list[str] = []

    def walk(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            if "name" in obj and isinstance(obj["name"], str):
                name = obj["name"]
                if not is_sentence_case(name):
                    issues.append(f"  entity{path}.name: {name!r}")
            for k, v in obj.items():
                if k != "name":
                    walk(v, f"{path}.{k}")

    walk(ref.get("entity", {}))

    if issues:
        print("Sentence-case FAILED — entity names must be sentence case:")
        for issue in issues:
            print(issue)
        print(
            "\nAllowlisted acronyms/proper nouns (always capitalized): "
            + ", ".join(sorted(ALWAYS_CAPS))
        )
        return 1
    print("OK — all entity names in strings.json are sentence case")
    return 0


if __name__ == "__main__":
    sys.exit(main())
