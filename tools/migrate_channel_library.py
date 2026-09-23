#!/usr/bin/env python3
"""Migrate a deployment onto the authoritative channel library.

Explicit, operator-run, and idempotent. NEVER triggered by a read, a page
visit, or plugin startup. Steps:

1. Read the actual legacy state: custom catalog, compiled networks, rollout
   file, publications. Record a manifest and an immutable backup OUTSIDE the
   install paths (default ``<data>/migration-backup-<stamp>/``).
2. Compile the selected seed (``data/proposed_channels_final.csv``) through
   the REAL importer into a staging artifact and validate it against the
   published preview (``analysis/just-watch-final/networks.preview.json``).
3. Build the target library: all 513 proposal networks (identities, seeds,
   sources, exclusions, and the proposal's five sanctioned exceptions
   preserved byte-for-byte), the proposal's initial groups, and every
   existing custom channel unchanged (ids, seeds, sources, policies) in the
   "My Channels" group.
4. Cross-check against the recorded migration expectations
   (``analysis/just-watch-final/migration_map.csv``): 292 retained
   identities, 221 new identities in reused slots, 282 vacant numbers.
   Unexpected drift is REPORTED and preserved in the backup — never
   silently overwritten.
5. Commit atomically under the library lock with a migration marker. A
   completed migration is a no-op on re-run (unless --force-recheck) and
   never resets subsequent owner edits.

Rollout entries for RETAINED network ids are preserved; entries for retired
ids are recorded and skipped explicitly (activation never transfers by
channel number). Publications of retired identities stay in the backup and
are excluded from scheduling (their ids no longer exist).

Usage:
    python3 tools/migrate_channel_library.py --data-dir <dir> --dry-run
    python3 tools/migrate_channel_library.py --data-dir <dir> --apply
    python3 tools/migrate_channel_library.py --data-dir <dir> --restore <backup-dir>
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from justwatch import continuing, library, networks  # noqa: E402
from tools.import_channels import import_csv, revision as networks_revision  # noqa: E402

PROPOSAL_CSV = ROOT / "data/proposed_channels_final.csv"
PROPOSAL_PREVIEW = ROOT / "analysis/just-watch-final/networks.preview.json"
MIGRATION_MAP = ROOT / "analysis/just-watch-final/migration_map.csv"
SEED_NAME = "just-watch-v4-final"

EXPECTED = {"keep": 292, "renumber_or_new": 221, "vacate": 282}


def log(message: str) -> None:
    print(f"migrate: {message}", file=sys.stderr)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def compile_seed(staging: Path) -> dict:
    """Compile the authoring CSV through the REAL importer into a staging
    artifact; validate against the published preview."""
    staging.mkdir(parents=True, exist_ok=True)
    compiled_path = staging / "networks.seed.json"
    compiled = import_csv(PROPOSAL_CSV)
    compiled["revision"] = networks_revision(compiled)
    compiled_path.write_text(
        json.dumps(compiled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
    preview = json.loads(PROPOSAL_PREVIEW.read_text(encoding="utf-8"))
    if len(compiled["channels"]) != 513:
        raise SystemExit(f"seed compiled to {len(compiled['channels'])} networks, expected 513")
    by_id = {c["id"]: c for c in compiled["channels"]}
    for row in preview["channels"]:
        mine = by_id.get(row["id"])
        if mine is None:
            raise SystemExit(f"seed is missing preview identity {row['id']}")
        for field in ("number", "name", "seed", "section", "source", "sort",
                      "programmingMode", "sourceLabel", "color", "glyph"):
            if mine.get(field) != row.get(field):
                raise SystemExit(
                    f"seed drift for {row['id']} field {field}: "
                    f"{mine.get(field)!r} != preview {row.get(field)!r}")
    sections = {}
    for row in compiled["channels"]:
        sections[row["section"]] = sections.get(row["section"], 0) + 1
    expected_sections = {"general": 209, "studios": 115, "performers": 189}
    if sections != expected_sections:
        raise SystemExit(f"section totals {sections} != expected {expected_sections}")
    log(f"seed verified: 513 networks, sections {sections}")
    return compiled


def read_migration_map() -> dict[str, dict]:
    rows = {}
    with MIGRATION_MAP.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows[int(row["current_number"])] = row
    return rows


def build_target(data_dir: Path, seed: dict) -> tuple[dict, dict]:
    """The target library document + a reconciliation report."""
    catalog_doc = json.loads((data_dir / "catalog.json").read_text(encoding="utf-8")) \
        if (data_dir / "catalog.json").exists() else {"channels": []}
    rollout = continuing.try_load_rollout(data_dir)
    rollout_ids = {str(x) for x in (rollout.get("networkIds") or [])}

    groups = [
        {"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
        {"id": "grp_general", "name": "General", "position": 2, "legacySection": "general"},
        {"id": "grp_studios", "name": "Studios", "position": 3, "legacySection": "studios"},
        {"id": "grp_performers", "name": "Performers", "position": 4, "legacySection": "performers"},
    ]
    channels = []
    for row in catalog_doc.get("channels", []):
        channels.append({
            "id": row["id"], "kind": "ch", "number": row["number"],
            "name": row["name"], "glyph": row.get("glyph"),
            "color": row.get("color", "#455A64"), "groupId": "grp_my",
            "sort": row.get("sort", "shuffle"), "seed": row["seed"],
            "enabled": row.get("enabled", True), "archived": False, "paused": False,
            "source": row.get("source") or {}, "sourceLabel": row.get("sourceLabel", ""),
            "programming": row.get("programming"),
            "provenance": {"origin": "custom"},
        })
    customs = {c["id"] for c in channels}

    retired_rollout = []
    for row in seed["channels"]:
        provenance = {
            "origin": "v4-final-proposal",
            "stableKey": row.get("stableKey") or f"{row['number']}|{row['name']}",
            "legacySection": row["section"],
            "family": row.get("family"),
            "seedCount": row.get("count"),
            "rationale": row.get("rationale"),
        }
        net = {
            "id": row["id"], "kind": "net", "number": row["number"],
            "name": row["name"], "glyph": row.get("glyph"),
            "color": row.get("color", "#455A64"),
            "groupId": {"general": "grp_general", "studios": "grp_studios",
                        "performers": "grp_performers"}[row["section"]],
            "sort": row.get("sort", "shuffle"), "seed": row["seed"],
            "enabled": True, "archived": False, "paused": False,
            "source": row["source"], "sourceLabel": row.get("sourceLabel", ""),
            "programming": ({"mode": row["programmingMode"]}
                            if row.get("programmingMode") else None),
            "provenance": provenance,
        }
        # Rollout activation is preserved FOR RETAINED IDS ONLY; activation
        # never transfers by channel number.
        if row["id"] in rollout_ids and row["id"] not in customs:
            pass  # kept implicitly: the rollout file itself is preserved
        channels.append(net)
    channels.sort(key=lambda c: c["number"])

    # --- drift reconciliation against the recorded migration map ---
    report = {"retained": 0, "new_at_reused_slot": 0, "vacant": 0, "drift": []}
    map_rows = list(read_migration_map().values())
    seed_numbers = {c["number"] for c in seed["channels"]}
    for row in map_rows:
        disposition = row["disposition"]
        number = int(row["current_number"])
        if disposition == "keep":
            report["retained"] += 1
            if number not in seed_numbers:
                report["drift"].append(
                    f"map says number {number} survives but the seed does not use it")
        elif disposition == "replace":
            report["new_at_reused_slot"] += 1
        elif disposition == "retire":
            report["vacant"] += 1
            if number in seed_numbers:
                report["drift"].append(
                    f"map says number {number} is vacated but the seed occupies it")
        else:
            report["drift"].append(f"unknown migration-map disposition {disposition!r}")
    # Any current deployment network NOT in the seed and NOT in the map is
    # drift worth preserving loudly.
    known_map_numbers = {int(r["current_number"]) for r in map_rows}
    deployment_networks = data_dir / "networks.json"
    if deployment_networks.exists():
        live = json.loads(deployment_networks.read_text(encoding="utf-8"))
    elif networks.PATH.exists():
        live = networks.load()
    else:
        live = {"channels": []}
    map_by_number = {int(r["current_number"]): r for r in map_rows}
    for row in live["channels"]:
        entry = map_by_number.get(row["number"])
        if entry is None:
            report["drift"].append(
                f"deployment has network {row['id']} #{row['number']} "
                f"({row['name']}) with no migration-map entry")
        elif entry["disposition"] == "keep" and entry["current_name"] != row["name"]:
            report["drift"].append(
                f"map expects #{row['number']} to be kept as {entry['current_name']!r} "
                f"but the deployment has {row['name']!r}")
    for entry in sorted(rollout_ids):
        seed_ids = {c["id"] for c in seed["channels"]}
        if entry and entry not in seed_ids:
            retired_rollout.append(entry)

    doc = {
        "schemaVersion": library.STORAGE_VERSION,
        "libraryId": f"lib_{hashlib.sha256(f'{SEED_NAME}:{time.time_ns()}'.encode()).hexdigest()[:10]}",
        "revision": 1,
        "migration": {
            "seedCatalog": SEED_NAME,
            "seedCsv": "data/proposed_channels_final.csv",
            "seedCsvDigest": sha256_file(PROPOSAL_CSV),
            "seedDigest": sha256_file(PROPOSAL_PREVIEW),
            "migratedAt": library._now_iso(),
            "legacyCatalogRevision": catalog_doc.get("revision", 0),
            "customCount": len(customs),
            "networkCount": len(seed["channels"]),
            "retiredRolloutIds": retired_rollout,
        },
        "groups": groups,
        "channels": channels,
        "settings": catalog_doc.get("settings", {}),
        "recentRequests": [],
    }
    errors, _ = library.validate_document(doc)
    if errors:
        raise SystemExit(f"target library failed validation: {errors[:5]}")
    return doc, report


def make_backup(data_dir: Path, backup_root: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_root / f"migration-backup-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    manifest = {"createdAt": library._now_iso(), "files": []}
    for name in ("catalog.json", "networks.json", "continuing-networks.json",
                 "channel-library.json"):
        src = data_dir / name
        if src.exists():
            shutil.copy2(src, target / name)
            manifest["files"].append({"name": name, "sha256": sha256_file(src)})
    for sub in ("programming", "snapshots"):
        src = data_dir / sub
        if src.is_dir():
            shutil.copytree(src, target / sub, dirs_exist_ok=True)
            manifest["files"].append({"name": f"{sub}/ (tree)"})
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"backup at {target} ({len(manifest['files'])} entries)")
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--staging-dir", type=Path,
                    default=ROOT / "analysis/channel-library-migration/staging")
    ap.add_argument("--backup-root", type=Path, default=None,
                    help="where backups go (default: the data dir's parent)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", type=Path, metavar="BACKUP_DIR")
    args = ap.parse_args()

    data_dir = args.data_dir.resolve()
    if not data_dir.exists():
        raise SystemExit(f"data dir {data_dir} does not exist")

    if args.restore:
        backup = args.restore.resolve()
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            name = entry["name"]
            src = backup / name
            if src.is_file() and not name.endswith("(tree)"):
                shutil.copy2(src, data_dir / Path(name).name)
        for sub in ("programming", "snapshots"):
            if (backup / sub).is_dir():
                shutil.copytree(backup / sub, data_dir / sub, dirs_exist_ok=True)
        library_file = data_dir / "channel-library.json"
        if library_file.exists() and not any(
                f["name"] == "channel-library.json" for f in manifest["files"]):
            library_file.unlink()
        log(f"restored {len(manifest['files'])} entries from {backup}")
        return 0

    seed = compile_seed(args.staging_dir)
    doc, report = build_target(data_dir, seed)
    log(f"target: {len(doc['channels'])} channels "
        f"({doc['migration']['customCount']} custom + "
        f"{doc['migration']['networkCount']} networks); "
        f"map reconciliation {report['retained']}/"
        f"{report['new_at_reused_slot']}/{report['vacant']} "
        f"(expected {EXPECTED['keep']}/{EXPECTED['renumber_or_new']}/{EXPECTED['vacate']})")
    if report["drift"]:
        for line in report["drift"][:20]:
            log(f"DRIFT: {line}")
        if len(report["drift"]) > 20:
            log(f"... and {len(report['drift']) - 20} more")
        retired = doc["migration"]["retiredRolloutIds"]
        if retired:
            log(f"retired rollout ids skipped explicitly: {retired}")

    if args.dry_run or not args.apply:
        log("dry run: nothing written" if args.dry_run
            else "no action requested (pass --apply or --dry-run)")
        return 0 if not report["drift"] or args.dry_run else 2

    if library.exists(data_dir):
        current = library.load(data_dir)
        marker = current.get("migration") or {}
        if marker.get("seedCatalog") == SEED_NAME:
            log(f"migration marker present (libraryId {current['libraryId']}, "
                f"revision {current['revision']}): NO-OP. Owner edits are preserved; "
                "use --restore to roll back deliberately.")
            return 0
        raise SystemExit("a channel library exists without a v4-final marker; "
                         "refusing to overwrite an unrecognized library")

    backup_root = args.backup_root or data_dir.parent
    make_backup(data_dir, backup_root)
    with library.library_lock(data_dir):
        if library.exists(data_dir):  # re-check under the lock
            log("library appeared concurrently; aborting")
            return 1
        library.save(data_dir, doc)
    log(f"committed library {doc['libraryId']} revision 1 "
        f"({len(doc['channels'])} channels). Legacy files remain as backed-up "
        "inputs; the plugin now serves the library.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
