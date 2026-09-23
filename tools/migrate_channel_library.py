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

from justwatch import catalog, continuing, library, networks  # noqa: E402
from tools.import_channels import import_csv, revision as networks_revision  # noqa: E402

PROPOSAL_CSV = ROOT / "data/proposed_channels_final.csv"
PROPOSAL_PREVIEW = ROOT / "analysis/just-watch-final/networks.preview.json"
MIGRATION_MAP = ROOT / "analysis/just-watch-final/migration_map.csv"
SEED_NAME = "just-watch-v4-final"

EXPECTED = {"keep": 292, "renumber_or_new": 221, "vacate": 282}

#: The managed restore scope: everything the migration/upgrade owns. A
#: restore recreates EXACTLY this state (recorded absences included) and
#: leaves unrelated operator data alone.
MANAGED_FILES = ("catalog.json", "networks.json", "continuing-networks.json",
                 "channel-library.json", "channel-library-pending.json")
MANAGED_DIRS = ("programming", "snapshots", "channel-library-history")
#: The compiled artifact may live OUTSIDE the data dir (the plugin install);
#: its ORIGINAL path is recorded per backup and restored there.
COMPILED_ARTIFACT = networks.PATH
RESTORE_JOURNAL = ".restore-journal.json"


def log(message: str) -> None:
    print(f"migrate: {message}", file=sys.stderr)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _tree_manifest(base: Path, rel: str) -> dict:
    entries = []
    for p in sorted((base / rel).rglob("*")):
        if p.is_file():
            entries.append({"path": str(p.relative_to(base / rel)),
                            "sha256": sha256_file(p)})
    return {"kind": "tree", "name": rel, "files": entries}


def make_backup(data_dir: Path, backup_root: Path) -> Path:
    """A complete, hash-verified snapshot of the managed scope — including
    recorded ABSENCES and the compiled artifact when it lives outside the
    data dir — in a collision-safe directory."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_root / f"migration-backup-{stamp}"
    suffix = 0
    while target.exists():
        suffix += 1
        target = backup_root / f"migration-backup-{stamp}-{suffix}"
    target.mkdir(parents=True)
    manifest = {"createdAt": library._now_iso(), "dataDir": str(data_dir), "files": []}
    for name in MANAGED_FILES:
        src = data_dir / name
        if src.is_file():
            shutil.copy2(src, target / name)
            manifest["files"].append({"name": name, "kind": "file",
                                      "sha256": sha256_file(src)})
        else:
            manifest["files"].append({"name": name, "kind": "file", "absent": True})
    for rel in MANAGED_DIRS:
        src = data_dir / rel
        if src.is_dir():
            shutil.copytree(src, target / rel)
            manifest["files"].append(_tree_manifest(target, rel))
        else:
            manifest["files"].append({"name": rel, "kind": "tree", "absent": True})
    external = COMPILED_ARTIFACT.resolve()
    in_data = (data_dir / "networks.json").resolve()
    if external.is_file() and external != in_data:
        shutil.copy2(external, target / "compiled-networks.json")
        manifest["files"].append({"name": "compiled-networks.json", "kind": "external",
                                  "originalPath": str(external),
                                  "sha256": sha256_file(external)})
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"backup at {target} ({len(manifest['files'])} managed entries)")
    return target


def _verify_backup(backup: Path) -> dict:
    """Fail closed on a tampered/incomplete backup: every recorded hash must
    match before restore touches anything."""
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest.get("files", []):
        if entry.get("absent"):
            continue
        if entry["kind"] == "tree":
            base = backup / entry["name"]
            listed = {f["path"] for f in entry.get("files", [])}
            for f in entry.get("files", []):
                p = base / f["path"]
                if not p.is_file() or sha256_file(p) != f["sha256"]:
                    raise SystemExit(f"backup tampered: {entry['name']}/{f['path']}")
            actual = {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}
            if actual != listed:
                raise SystemExit(f"backup tampered: {entry['name']} file set drift")
        else:
            p = backup / entry["name"]
            if not p.is_file() or sha256_file(p) != entry["sha256"]:
                raise SystemExit(f"backup tampered: {entry['name']}")
    return manifest


def restore(data_dir: Path, backup: Path) -> int:
    """EXACT restore of the managed scope: recorded files return (hash-
    verified), recorded ABSENCES are recreated (post-backup artifacts are
    removed), managed trees are REPLACED (not merged), and unrelated operator
    data is untouched. Staged + journaled: an interrupted restore is
    resumable by re-running the same command."""
    manifest = _verify_backup(backup)
    steps: list[dict] = []
    for entry in manifest["files"]:
        name = entry["name"]
        if entry.get("kind") == "external":
            steps.append({"op": "external", "name": name,
                          "dest": entry["originalPath"], "absent": False})
            continue
        steps.append({"op": entry["kind"], "name": name, "absent": bool(entry.get("absent"))})
    journal_path = data_dir / RESTORE_JOURNAL
    done: set[str] = set()
    if journal_path.exists():
        try:
            done = set(json.loads(journal_path.read_text(encoding="utf-8")).get("done", []))
        except (OSError, json.JSONDecodeError):
            done = set()
    with library.library_lock(data_dir):
        with catalog.catalog_lock(data_dir):
            staged = data_dir / ".restore-staging"
            if staged.exists():
                shutil.rmtree(staged)
            staged.mkdir()
            try:
                for step in steps:
                    if step["name"] in done:
                        continue
                    name = step["name"]
                    if step["op"] == "external":
                        dest = Path(step["dest"])
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup / name, dest)
                    elif step.get("absent"):
                        victim = data_dir / name
                        if victim.is_dir():
                            shutil.rmtree(victim)
                        elif victim.exists():
                            victim.unlink()
                    elif step["op"] == "tree":
                        # replace the WHOLE managed tree: never merge newer
                        # publications/snapshots over the restored state
                        stage = staged / name
                        shutil.copytree(backup / name, stage)
                        victim = data_dir / name
                        if victim.is_dir():
                            shutil.rmtree(victim)
                        shutil.move(str(stage), str(victim))
                    else:
                        shutil.copy2(backup / name, data_dir / name)
                    done.add(name)
                    journal_path.write_text(
                        json.dumps({"done": sorted(done),
                                    "backup": str(backup)}), encoding="utf-8")
            finally:
                shutil.rmtree(staged, ignore_errors=True)
                journal_path.unlink(missing_ok=True)
    log(f"restored {len(done)} managed entries exactly from {backup}")
    return 0


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
    # Strict loader: a corrupt/future-schema catalog is a hard stop, never a
    # silently-empty migration input (audit C10).
    catalog_doc = catalog.load(data_dir)
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
    # drift worth preserving loudly. Read through the strict loaders.
    known_map_numbers = {int(r["current_number"]) for r in map_rows}
    deployment_networks = data_dir / "networks.json"
    if deployment_networks.exists():
        live = networks.load(deployment_networks)
    elif networks.PATH.exists():
        live = networks.load()
    else:
        live = {"channels": []}
    map_by_number = {int(r["current_number"]): r for r in map_rows}
    live_by_number = {row["number"]: row for row in live["channels"]}
    seed_by_number = {row["number"]: row for row in seed["channels"]}
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
        elif entry["disposition"] == "keep":
            # Identity reconciliation for retained rows: the deployment's
            # channel at a kept number must BE the seed identity (same id and
            # seed), not merely share its display name (audit C10).
            final = seed_by_number.get(row["number"])
            if final is not None and (final["id"] != row["id"]
                                      or final["seed"] != row.get("seed")):
                report["drift"].append(
                    f"#{row['number']} is marked kept but its identity changed: "
                    f"deployment {row['id']}/{row.get('seed')} != seed "
                    f"{final['id']}/{final['seed']}")
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


def _read_legacy_catalog(data_dir: Path) -> dict:
    """The legacy catalog through its STRICT loader — a corrupt/future-schema
    file is a hard stop, never a silent empty migration input."""
    return catalog.load(data_dir)


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
        if not (backup / "manifest.json").is_file():
            raise SystemExit(f"{backup} is not a migration backup (no manifest)")
        return restore(data_dir, backup)

    # An already-migrated deployment is a NO-OP before anything else: a
    # repeated seed migration can never recompile away owner edits, and
    # post-migration drift in the legacy inputs is irrelevant by design.
    if library.exists(data_dir):
        current = library.load(data_dir)
        marker = current.get("migration") or {}
        if marker.get("seedCatalog") == SEED_NAME:
            log(f"migration marker present (libraryId {current['libraryId']}, "
                f"revision {current['revision']}): NO-OP. Owner edits are "
                "preserved; use --restore to roll back deliberately.")
            return 0
        if args.apply:
            raise SystemExit("a channel library exists without a v4-final marker; "
                             "refusing to overwrite an unrecognized library")

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
        # FAIL CLOSED: drift means nonzero exit for dry-run AND apply, with
        # no authoritative state written (audit C10 — the old tool reported
        # drift and committed anyway).
        log("drift detected: refusing to migrate. Reconcile the deployment "
            "(or re-run against the data it was recorded from); nothing was written.")
        return 2

    if args.dry_run or not args.apply:
        log("dry run: nothing written" if args.dry_run
            else "no action requested (pass --apply or --dry-run)")
        return 0

    if library.exists(data_dir):
        raise SystemExit("a channel library exists without a v4-final marker; "
                         "refusing to overwrite an unrecognized library")

    backup_root = args.backup_root or data_dir.parent
    make_backup(data_dir, backup_root)
    # Legacy inputs are digested before the commit and re-checked under the
    # lock so a concurrent SaveCatalog/scheduler write between snapshot and
    # commit cannot be silently lost (audit C10).
    legacy_digests = {
        name: (sha256_file(data_dir / name) if (data_dir / name).is_file() else None)
        for name in ("catalog.json", "networks.json", "continuing-networks.json")
    }
    with library.library_lock(data_dir):
        if library.exists(data_dir):  # re-check under the lock
            log("library appeared concurrently; aborting")
            return 1
        for name, digest in legacy_digests.items():
            src = data_dir / name
            current_digest = sha256_file(src) if src.is_file() else None
            if current_digest != digest:
                log(f"legacy input {name} changed during migration; aborting "
                    "with nothing written")
                return 1
        library.save(data_dir, doc)
    log(f"committed library {doc['libraryId']} revision 1 "
        f"({len(doc['channels'])} channels). Legacy files remain as backed-up "
        "inputs; the plugin now serves the library.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
