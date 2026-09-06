#!/usr/bin/env python3
"""Recount the channel CSV against its Stash library, and apply tag policy.

``exact_scene_count`` is the network tier's health data: the importer trusts
it verbatim, so it must be recomputed whenever a row's criteria change — and
periodically swept, because the library drifts. This tool is that loop:

* ``--exclude-tag ID=NAME`` (repeatable) appends a tag to every row's any-of
  exclusion columns before recounting — the "this tag airs nowhere in the
  network tier" policy knob (e.g. JAV, confined to its own custom channel).
* recounting projects each row exactly as the runtime does (the importer's
  ``build_source`` -> ``lineup.build_scene_filter``), so counts and aired
  lineups can never disagree about membership.
* ``--write`` rewrites ``exact_scene_count`` + ``library_share_pct`` in the
  CSV; without it the run is a report. ``--check`` asserts zero drift and
  exits non-zero otherwise (the re-runnable validation sweep).

Counts are only meaningful against the library the CSV's ids belong to (they
are Stash database ids); run this against that server. Credentials come from
a file, never argv: ``--url``/``--api-key-file`` default to the STASH_URL and
STASH_API_KEY_FILE environment variables.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO / "data" / "proposed_channels_scene_validated.csv"

# Run as a script from tools/, the justwatch package lives at the repo root.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Rows recounting into fewer scenes than this are flagged for review (a
#: near-zero network is usually a wrong criterion, not a channel to keep).
THIN = 5

COUNT_QUERY = """
query JustWatchRecount($filter: FindFilterType!, $scene_filter: SceneFilterType!) {
  findScenes(filter: $filter, scene_filter: $scene_filter) { count }
}
"""

TOTAL_QUERY = """
query JustWatchTotal($filter: FindFilterType!) {
  findScenes(filter: $filter) { count }
}
"""

PAGE = {"page": 1, "per_page": 0}


def fail(message: str) -> None:
    print(f"recount_channels: {message}", file=sys.stderr)
    raise SystemExit(1)


def _importer():
    """The importer module, loaded by path like the tests do (tools/ is not a package)."""
    module = sys.modules.get("import_channels")
    if module is None:
        spec = importlib.util.spec_from_file_location(
            "import_channels", REPO / "tools" / "import_channels.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["import_channels"] = module
        spec.loader.exec_module(module)
    return module


def build_client(url: str, api_key: str):
    from justwatch.stash_client import StashClient

    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        fail(f"--url must be an absolute origin, got {url!r}")
    return StashClient(
        {"Scheme": parts.scheme, "Host": parts.hostname, "Port": parts.port},
        settings={"stash_api_key": api_key},
    )


def parse_policies(values: list[str]) -> list[tuple[str, str]]:
    policies: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        tag_id, sep, name = value.partition("=")
        tag_id, name = tag_id.strip(), name.strip()
        if not sep or not tag_id.isdigit() or not name:
            fail(f"--exclude-tag expects ID=NAME, got {value!r}")
        if tag_id in seen:
            fail(f"--exclude-tag {tag_id} given twice")
        seen.add(tag_id)
        policies.append((tag_id, name))
    return policies


def apply_exclusions(rows: list[dict], policies: list[tuple[str, str]]) -> int:
    """Append each policy tag to every row's exclusion columns, idempotently.

    Ids and names stay aligned because they are only ever appended as a pair.
    Returns the number of rows changed."""
    changed = 0
    for row in rows:
        ids = [part for part in (row["exclude_tag_ids_any"] or "").split("|") if part]
        names = [part for part in (row["exclude_tags_any"] or "").split("|") if part]
        before = len(ids)
        for tag_id, name in policies:
            if tag_id not in ids:
                ids.append(tag_id)
                names.append(name)
        if len(ids) != before:
            row["exclude_tag_ids_any"] = "|".join(ids)
            row["exclude_tags_any"] = "|".join(names)
            row["exclude_tag_logic"] = "ANY"
            changed += 1
    return changed


def recount_rows(client, rows: list[dict]) -> list[tuple[dict, int, int]]:
    """Count every row once, stamping ``row['_live_count']``.

    Returns the drifted rows as ``(row, csv_count, live_count)``; ``--write``
    consumes the stamps so the CSV rewrite never re-queries."""
    importer = _importer()
    from justwatch import lineup

    drifted: list[tuple[dict, int, int]] = []
    for index, row in enumerate(rows, start=1):
        if index % 100 == 0:
            print(f"counting {index}/{len(rows)}", file=sys.stderr)
        source = importer.build_source(row)
        scene_filter = lineup.build_scene_filter(source)
        data = client.submit(COUNT_QUERY, {"filter": PAGE, "scene_filter": scene_filter})
        live = int((data.get("findScenes") or {}).get("count") or 0)
        row["_live_count"] = live
        stored = int(str(row["exact_scene_count"]).strip())
        if live != stored:
            drifted.append((row, stored, live))
    return drifted


def write_csv(path: Path, rows: list[dict], fieldnames: list[str], total: int) -> None:
    for row in rows:
        live = row["_live_count"]
        row["exact_scene_count"] = str(live)
        if "library_share_pct" in row:
            row["library_share_pct"] = f"{live / total * 100:.3f}"
        del row["_live_count"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.getenv("STASH_URL"),
                        help="Stash GraphQL origin the CSV's ids belong to")
    parser.add_argument("--api-key-file", default=os.getenv("STASH_API_KEY_FILE"),
                        help="file holding the Stash API key")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--exclude-tag", action="append", default=[], metavar="ID=NAME",
                        help="append this tag to every row's exclusions (repeatable)")
    parser.add_argument("--write", action="store_true",
                        help="rewrite counts (+ shares) into the CSV; default reports only")
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero on any drift (validation sweep; no write)")
    args = parser.parse_args(argv)

    if not args.url or urlsplit(args.url).scheme not in ("http", "https"):
        fail("set STASH_URL or --url")
    if not args.api_key_file:
        fail("set STASH_API_KEY_FILE or --api-key-file")
    if args.check and args.write:
        fail("--check and --write are mutually exclusive")
    csv_path = args.csv if args.csv.is_absolute() else REPO / args.csv
    if not csv_path.exists():
        fail(f"no CSV at {csv_path}")

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    if not rows:
        fail("CSV has no rows")

    policies = parse_policies(args.exclude_tag)
    if policies:
        changed = apply_exclusions(rows, policies)
        summary = ", ".join(f"{tag_id} ({name})" for tag_id, name in policies)
        print(f"recount_channels: excluded {summary} on {changed}/{len(rows)} rows")

    try:
        api_key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
        client = build_client(args.url, api_key)
        data = client.submit(TOTAL_QUERY, {"filter": PAGE})
        total = int((data.get("findScenes") or {}).get("count") or 0)
    except OSError as exc:
        fail(f"cannot read api key file {args.api_key_file}: {exc}")

    drifted = recount_rows(client, rows)
    print(f"recount_channels: {len(rows)} rows against {args.url} "
          f"({total} scenes): {len(drifted)} drifted")
    net = 0
    for row, stored, live in drifted:
        net += live - stored
        print(f"  {row['channel_number']} {row['channel_name']}: {stored} -> {live}")
    if net:
        print(f"recount_channels: net {net:+d} scenes across drifted rows")

    thin = [
        (row, stored, live) for row, stored, live in drifted
        if live < THIN and stored >= THIN
    ]
    for row, stored, live in thin:
        print(f"recount_channels: REVIEW row {row['channel_number']} "
              f"{row['channel_name']} fell below {THIN} scenes ({stored} -> {live})")

    if args.check:
        if drifted:
            print("recount_channels: CHECK FAILED", file=sys.stderr)
            return 1
        print("recount_channels: check passed")
        return 0

    if args.write:
        write_csv(csv_path, rows, fieldnames, total)
        print(f"recount_channels: wrote {csv_path}")
    elif drifted:
        print("recount_channels: drift reported; rerun with --write to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
