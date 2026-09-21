#!/usr/bin/env python3
"""Rollback helpers for continuing programming (dev/operator tooling).

Two independent rollback levers, both safe to run while Stash is up:

* ``--deactivate``  writes ``{"enabled": false}`` into the rollout file, the
  operational kill switch: sync reads (Directory/Schedule) immediately
  advertise fixed mode again and the next preparation task clears the
  scheduled set. Publications and durable state are left in place, so
  re-activation resumes without losing consumption history.
* ``--restore-v2-backups`` copies every ``<channel>.json.v2.bak`` written by
  the schema-2 → schema-3 migration back over the live publication. Use this
  only to return to a PRE-remediation plugin build: the current engine reads
  schema 2 for migration, so a restored backup is migrated forward again on
  the next prepare unless the plugin is also downgraded.

Neither touches catalog.json, networks.json, or custom (schema 1)
publications.
"""
import argparse
import json
import sys
from pathlib import Path


def deactivate(data_dir: Path) -> None:
    rollout = data_dir / "continuing-networks.json"
    payload = {"enabled": False, "networkIds": [], "stage": "active"}
    if rollout.exists():
        try:
            raw = json.loads(rollout.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw["enabled"] = False
                payload = raw
        except json.JSONDecodeError:
            pass  # a broken rollout already reads as deactivated
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"rollout deactivated: {rollout}")


def restore_v2_backups(data_dir: Path, apply: bool) -> int:
    programming = data_dir / "programming"
    if not programming.is_dir():
        print("no programming directory; nothing to restore")
        return 0
    restored = 0
    for backup in sorted(programming.glob("*.json.v2.bak")):
        live = backup.with_name(backup.name[: -len(".v2.bak")])
        action = "restored" if apply else "would restore"
        if apply:
            backup.replace(live)
        print(f"{action}: {live.name} <- {backup.name}")
        restored += 1
    print(f"{restored} backup(s) {'restored' if apply else 'found'}")
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path,
                        help="the Stash data dir (contains programming/)")
    parser.add_argument("--deactivate", action="store_true",
                        help="write the rollout kill switch (enabled=false)")
    parser.add_argument("--restore-v2-backups", action="store_true",
                        help="restore schema-2 backups over live publications")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --restore-v2-backups: only list what would be restored")
    args = parser.parse_args()
    if not args.deactivate and not args.restore_v2_backups:
        parser.error("choose --deactivate and/or --restore-v2-backups")
    if args.deactivate:
        deactivate(args.data_dir)
    if args.restore_v2_backups:
        restore_v2_backups(args.data_dir, apply=not args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
