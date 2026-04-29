#!/usr/bin/env python3
"""
Einmalige Migration: verschiebt vorhandene Bosch-Event-Dateien auf einer
SMB-Freigabe von der Struktur ``{base}/{year}/{month}/`` in
``{base}/{year}/{month}/{day}/``.

Tag wird aus dem Dateinamen geparst (Default-Pattern
``{camera}_{date}_{time}_{type}_{id}.{ext}`` mit ``date = YYYY-MM-DD``)
oder, wenn der Filename nicht passt, aus der ``mtime`` der Datei.

Aufruf auf dem Home Assistant Host:

    # Trockendurchlauf (default — nur Logging, keine Schreiboperationen):
    python3 migrate_smb_to_day_folders.py

    # Tatsächlich verschieben:
    python3 migrate_smb_to_day_folders.py --apply

Liest die SMB-Zugangsdaten aus dem aktiven HA Config Entry der
``bosch_shc_camera`` Integration (``/config/.storage/core.config_entries``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

DOMAIN = "bosch_shc_camera"
DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_")  # matches "_YYYY-MM-DD_" inside filename
YEAR_RE = re.compile(r"^\d{4}$")
MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])$")
DAY_RE = re.compile(r"^(0[1-9]|[12]\d|3[01])$")


def load_smb_options(config_dir: Path) -> dict:
    entries_path = config_dir / ".storage" / "core.config_entries"
    if not entries_path.exists():
        sys.exit(f"core.config_entries not found at {entries_path}")
    data = json.loads(entries_path.read_text(encoding="utf-8"))
    for entry in data.get("data", {}).get("entries", []):
        if entry.get("domain") == DOMAIN:
            opts = entry.get("options") or {}
            if opts.get("enable_smb_upload") and opts.get("smb_server"):
                return opts
    sys.exit(f"No active {DOMAIN} config entry with SMB upload enabled.")


def parse_day_from_filename(name: str) -> str | None:
    m = DATE_RE.search(name)
    return m.group(1)[8:10] if m else None


def parse_day_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%d")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Tatsächlich verschieben. Ohne diesen Flag nur Trockendurchlauf.")
    ap.add_argument("--config-dir", default="/config",
                    help="HA-Config-Verzeichnis (default: /config)")
    args = ap.parse_args()

    try:
        from smbclient import (
            register_session, scandir, rename, mkdir, stat as smb_stat,
        )
    except ImportError:
        sys.exit("smbprotocol/smbclient not installed in this Python env. "
                 "On HA Core: 'pip install smbprotocol' inside the HA venv, "
                 "or run from a Python env that has it.")

    opts = load_smb_options(Path(args.config_dir))
    server = opts["smb_server"].strip()
    share = opts["smb_share"].strip()
    username = opts.get("smb_username", "").strip()
    password = opts.get("smb_password", "")
    base = opts.get("smb_base_path", "Bosch-Kameras").strip()

    print(f"[i] target: \\\\{server}\\{share}\\{base}")
    print(f"[i] mode:   {'APPLY (will move files)' if args.apply else 'DRY-RUN'}")
    register_session(server, username=username, password=password)

    root = f"\\\\{server}\\{share}\\{base}"
    moved = 0
    skipped = 0
    errors = 0

    for year_entry in scandir(root):
        if not year_entry.is_dir() or not YEAR_RE.match(year_entry.name):
            continue
        year_path = f"{root}\\{year_entry.name}"

        for month_entry in scandir(year_path):
            if not month_entry.is_dir() or not MONTH_RE.match(month_entry.name):
                continue
            month_path = f"{year_path}\\{month_entry.name}"

            files_in_month = [e for e in scandir(month_path) if not e.is_dir()]
            if not files_in_month:
                continue
            print(f"[.] {year_entry.name}/{month_entry.name}: {len(files_in_month)} file(s)")

            for f in files_in_month:
                day = parse_day_from_filename(f.name)
                if day is None:
                    try:
                        st = smb_stat(f"{month_path}\\{f.name}")
                        day = parse_day_from_mtime(st.st_mtime)
                    except Exception as err:
                        print(f"  [!] cannot determine day for {f.name}: {err}")
                        errors += 1
                        continue

                if not DAY_RE.match(day):
                    print(f"  [!] invalid day '{day}' for {f.name}")
                    errors += 1
                    continue

                day_path = f"{month_path}\\{day}"
                src = f"{month_path}\\{f.name}"
                dst = f"{day_path}\\{f.name}"

                # already in correct place? (shouldn't happen but be safe)
                try:
                    smb_stat(dst)
                    print(f"  [skip] target already exists: {f.name}")
                    skipped += 1
                    continue
                except OSError:
                    pass

                if args.apply:
                    try:
                        try:
                            smb_stat(day_path)
                        except OSError:
                            mkdir(day_path)
                        rename(src, dst)
                        moved += 1
                    except Exception as err:
                        print(f"  [!] move failed {f.name}: {err}")
                        errors += 1
                else:
                    print(f"  [dry] {f.name} -> {day}/")
                    moved += 1

            time.sleep(0.05)  # polite to the share

    summary = (f"[=] {'moved' if args.apply else 'would move'}: {moved}, "
               f"skipped: {skipped}, errors: {errors}")
    print(summary)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
