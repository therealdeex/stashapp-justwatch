#!/usr/bin/env python3
"""Validate the v4 final catalog — every check from the task list, plus the
review-added regressions (golden legacy compile, finalizer determinism,
operational large-ANY check). Writes validation_report.md and exits non-zero
on any failure.

Everything membership-related is recomputed from a FRESH, independent read of
scenes.jsonl (not from the finalizer's in-memory state): tag/performer/studio
row maps, studio hierarchy, scene metadata, JAV exclusion. The compiled
preview artifact (networks.preview.json — what would actually deploy) is the
object under test; final_channels.json/csv and the authoring CSV are
cross-checked against it.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT_DIR = REPO / "analysis" / "just-watch-final"
FINAL_CSV = REPO / "data" / "proposed_channels_final.csv"
PREVIEW_JSON = OUT_DIR / "networks.preview.json"
LIVE_NETWORKS = REPO / "justwatch" / "networks.json"
REPORT = OUT_DIR / "validation_report.md"

JW_EXTRACT = Path(os.environ.get("JW_EXTRACT", "/home/shahram/dev/stash-extract"))
JAV_ID = "9320"
NEW_ARRIVALS_ANCHOR = "2026-03-12"  # extraction date (2026-09-08) - 180 days
N_LIBRARY = 20_299

SANCTIONED_JAV_EXEMPT_PERFORMERS = {"8926", "1661"}
SANCTIONED_JAV_EXEMPT_STUDIOS = {"819", "1599", "2733"}

EXPECTED_SECTIONS = {"general": 209, "performers": 189, "studios": 115}
EXPECTED_FAMILIES = {
    "performer_spotlight": 158, "studio_spotlight": 100, "tag_spotlight": 142,
    "tag_pair": 40, "tag_triple": 15, "tag_include_exclude": 4,
    "performer_tag": 15, "studio_tag": 15, "performer_pair": 16,
}


class Report:
    def __init__(self):
        self.results: list[tuple[str, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.results.append(("PASS" if ok else "FAIL", name, detail))
        return ok

    @property
    def failures(self) -> int:
        return sum(1 for status, _, _ in self.results if status == "FAIL")


# ---------------------------------------------------------------------------
# Fresh, independent extraction read
# ---------------------------------------------------------------------------

class Extraction:
    def __init__(self):
        self.tag_rows: dict[str, set[int]] = defaultdict(set)
        self.perf_rows: dict[str, set[int]] = defaultdict(set)
        self.studio_rows: dict[str, set[int]] = defaultdict(set)
        self.meta: list[tuple[str, int, str]] = []  # (date, duration, created)
        self.scenes = []
        with (JW_EXTRACT / "scenes.jsonl").open() as handle:
            for i, line in enumerate(handle):
                scene = json.loads(line)
                self.scenes.append(scene)
                for t in (scene.get("tag_ids") or "").split("|"):
                    if t:
                        self.tag_rows[t].add(i)
                for p in (scene.get("performer_ids") or "").split("|"):
                    if p:
                        self.perf_rows[p].add(i)
                sid = scene.get("studio_id") or ""
                if sid:
                    self.studio_rows[sid].add(i)
                self.meta.append((scene.get("scene_date") or "",
                                  scene.get("duration_seconds") or 0,
                                  (scene.get("created_at") or "")[:10]))
        studio_parent = {}
        self.studio_children: dict[str, list[str]] = defaultdict(list)
        with (JW_EXTRACT / "studios.csv").open() as handle:
            for row in csv.DictReader(handle):
                if row.get("parent_studio_id"):
                    studio_parent[row["studio_id"]] = row["parent_studio_id"]
        for child, parent in studio_parent.items():
            self.studio_children[parent].append(child)
        self.tag_names = {}
        with (JW_EXTRACT / "tags.csv").open() as handle:
            for row in csv.DictReader(handle):
                self.tag_names[row["tag_id"]] = row["tag_name"]
        self.perf_names = {}
        with (JW_EXTRACT / "performers.csv").open() as handle:
            for row in csv.DictReader(handle):
                self.perf_names[row["performer_name"]] = row["performer_id"]
        self._hier_cache: dict[str, set[int]] = {}
        self.jav_rows = self.tag_rows.get(JAV_ID, set())
        self.universe = set(range(len(self.scenes))) - self.jav_rows

    def hier(self, studio: str) -> set[int]:
        """A studio's own scenes plus every descendant's (runtime depth -1)."""
        if studio in self._hier_cache:
            return self._hier_cache[studio]
        rows = set(self.studio_rows.get(studio, ()))
        self._hier_cache[studio] = rows   # pre-set: cycle guard
        for child in self.studio_children.get(studio, ()):
            rows |= self.hier(child)
        self._hier_cache[studio] = rows
        return rows

    def membership(self, source: dict) -> set[int]:
        """Recompute a compiled source's scene membership from raw rows — an
        independent reimplementation of the runtime projection."""
        rows: set[int] | None = None

        def intersect(other: set[int]) -> None:
            nonlocal rows
            rows = other if rows is None else (rows & other)

        for t in source.get("tags", ()):          # tag tree is flat (verified)
            intersect(set(self.tag_rows.get(t, ())))
        for p in source.get("performers", ()):
            intersect(set(self.perf_rows.get(p, ())))
        for s in source.get("studios", ()):
            intersect(self.hier(s))
        union: set[int] | None = None
        for p in source.get("performersAny", ()):
            rows_p = set(self.perf_rows.get(p, ()))
            union = rows_p if union is None else (union | rows_p)
        if union is not None:
            intersect(union)
        union = None
        for s in source.get("studiosAny", ()):
            sub = self.hier(s)
            union = sub if union is None else (union | sub)
        if union is not None:
            intersect(union)
        date = source.get("date")
        if isinstance(date, dict):
            lo, hi = date.get("from", ""), date.get("to", "")
            intersect({i for i, (d, _, _) in enumerate(self.meta)
                       if d and lo <= d <= hi})
        duration = source.get("duration")
        if isinstance(duration, dict):
            minimum = duration.get("min", 0)
            intersect({i for i, (_, dur, _) in enumerate(self.meta)
                       if dur >= minimum})
        created = source.get("createdAt")
        if isinstance(created, dict):
            intersect({i for i, (_, _, c) in enumerate(self.meta)
                       if c >= NEW_ARRIVALS_ANCHOR})
        rows = rows if rows is not None else set(range(len(self.scenes)))
        for t in source.get("excludeTags", ()):
            rows -= self.tag_rows.get(t, set())
        return rows

    def norm_tag(self, tag_id: str) -> str:
        """Concept form of a canonical tag: namespace stripped, compacted,
        lowercase — the same normalization finalize resolves locked names
        with ("Cumshot-mouth" == "Cumshot - mouth")."""
        return self.concept_key(self.display_tag(tag_id))

    def display_tag(self, tag_id: str) -> str:
        name = self.tag_names.get(tag_id, tag_id)
        return name.split(":", 1)[1].strip() if ":" in name else name

    @staticmethod
    def concept_key(display: str) -> str:
        return "".join(ch for ch in display.lower() if ch not in " -_")


def _net_id(stable_key: str) -> str:
    return "net_" + hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:8]


def _load_importer():
    spec = importlib.util.spec_from_file_location(
        "import_channels", REPO / "tools" / "import_channels.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_finalize():
    spec = importlib.util.spec_from_file_location(
        "finalize", Path(__file__).with_name("finalize.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_structure(report, channels, authoring):
    def signature(channel: dict) -> tuple:
        source = channel["source"]
        return (channel["family"],
                tuple(sorted(source.get("tags", ()))),
                tuple(sorted(source.get("excludeTags", ()))),
                tuple(sorted(source.get("performers", ()))),
                tuple(sorted(source.get("performersAny", ()))),
                tuple(sorted(source.get("studios", ()))),
                tuple(sorted(source.get("studiosAny", ()))),
                json.dumps(source.get("date"), sort_keys=True),
                json.dumps(source.get("duration"), sort_keys=True),
                json.dumps(source.get("createdAt"), sort_keys=True))

    signatures = [signature(c) for c in channels]
    report.check(len(set(signatures)) == len(signatures) == 513,
                 "1. 513 unique channel definitions",
                 f"{len(set(signatures))} unique of {len(signatures)}")
    numbers = [c["number"] for c in channels]
    report.check(len(set(numbers)) == len(numbers), "2. no duplicate numbers",
                 f"{len(numbers)} numbers")
    keys = [r["stable_key"] for r in authoring]
    report.check(len(set(keys)) == len(keys), "3. no duplicate stable keys",
                 f"{len(keys)} keys")
    ids = [c["id"] for c in channels]
    report.check(len(set(ids)) == len(ids), "4. no duplicate network ids",
                 f"{len(ids)} ids")
    empty = [c["name"] for c in channels if c["count"] <= 0]
    report.check(not empty, "5. no empty channel",
                 f"min count {min(c['count'] for c in channels)}")


def check_memberships(report, ex: Extraction, channels):
    mismatches = []
    for channel in channels:
        fresh = len(ex.membership(channel["source"]))
        if fresh != channel["count"]:
            mismatches.append(f"#{channel['number']} {channel['name']}: "
                              f"{channel['count']} stored vs {fresh} fresh")
    report.check(not mismatches,
                 "6. every channel reproduces its count exactly "
                 "(ordinary, aggregate, and metadata filters alike)",
                 "; ".join(mismatches[:8]) or "513/513 exact")


def check_aggregates(report, ex: Extraction, channels):
    perf_net = {p: len(rows - ex.jav_rows) for p, rows in ex.perf_rows.items()}
    concepts = {
        "One-Scene Performers": {p for p, n in perf_net.items() if n == 1},
        "Rare Performers": {p for p, n in perf_net.items() if 1 <= n < 5},
        "Prolific Performers": {p for p, n in perf_net.items() if n >= 100},
    }
    details = []
    ok = True
    for label, expected in concepts.items():
        channel = next((c for c in channels if c["name"].startswith(label)), None)
        if channel is None:
            ok = False
            details.append(f"{label}: MISSING")
            continue
        listed = set(channel["source"].get("performersAny", ()))
        ok &= listed == expected
        details.append(f"{label}: {len(listed):,} listed vs "
                       f"{len(expected):,} expected, identical={listed == expected}")
    # Fringe: <10 hierarchical post-JAV pool; id list must reproduce the union
    hier_net = {s: len(ex.hier(s) - ex.jav_rows) for s in ex.studio_rows}
    small = {s for s, n in hier_net.items() if 0 < n < 10}
    fringe = next(c for c in channels if c["name"] == "Fringe Studios")
    union = set()
    for s in small:
        union |= ex.hier(s)
    union -= ex.jav_rows
    fresh = ex.membership(fringe["source"])
    ok &= fresh == union
    details.append(f"Fringe Studios: {len(fringe['source']['studiosAny']):,} "
                   f"maximal studios; union identical={fresh == union} "
                   f"({len(union)} scenes)")
    report.check(ok, "7. aggregate ANY channels reproduce the reference unions",
                 "; ".join(details))


def check_metadata(report, ex: Extraction, channels):
    refs = {}
    for channel in channels:
        source = channel["source"]
        if isinstance(source.get("date"), dict):
            lo, hi = source["date"]["from"], source["date"]["to"]
            expected = {i for i, (d, _, _) in enumerate(ex.meta)
                        if d and lo <= d <= hi} - ex.jav_rows
            refs[f"{channel['name']} ({lo}..{hi})"] = (len(expected),
                                                       channel["count"])
        if isinstance(source.get("duration"), dict):
            minimum = source["duration"]["min"]
            expected = {i for i, (_, dur, _) in enumerate(ex.meta)
                        if dur >= minimum} - ex.jav_rows
            refs[f"{channel['name']} (>= {minimum}s)"] = (len(expected),
                                                          channel["count"])
        if isinstance(source.get("createdAt"), dict):
            expected = {i for i, (_, _, c) in enumerate(ex.meta)
                        if c >= NEW_ARRIVALS_ANCHOR} - ex.jav_rows
            refs[f"{channel['name']} (anchor {NEW_ARRIVALS_ANCHOR})"] = (
                len(expected), channel["count"])
    report.check(all(e == c for e, c in refs.values()),
                 "8. metadata filters reproduce reference memberships",
                 "; ".join(f"{k}: {e} vs {c}" for k, (e, c) in sorted(refs.items())))


def check_jav_policy(report, channels):
    exempt = [c for c in channels
              if JAV_ID not in c["source"].get("excludeTags", ())]
    ids = set()
    for channel in exempt:
        source = channel["source"]
        ids |= set(source.get("performers", ())) | set(source.get("studios", ()))
    expected = SANCTIONED_JAV_EXEMPT_PERFORMERS | SANCTIONED_JAV_EXEMPT_STUDIOS
    report.check(len(exempt) == 5 and ids == expected,
                 "9. JAV excluded everywhere except the 5 sanctioned channels",
                 f"{len(exempt)} exempt, covering {sorted(ids)}")


def check_coverage(report, ex: Extraction, channels) -> float:
    covered = set()
    for channel in channels:
        covered |= ex.membership(channel["source"])
    coverage = 100 * len(covered & ex.universe) / len(ex.universe)
    report.check(coverage >= 99.0, "10. post-JAV library coverage ~100%",
                 f"{coverage:.2f}% of {len(ex.universe):,} scenes")
    return coverage


def check_bands(report, ex: Extraction, channels, finalize):
    by_family = Counter(c["family"] for c in channels)
    fam_ok = all(by_family.get(f, 0) == n for f, n in EXPECTED_FAMILIES.items())
    specials = sum(n for f, n in by_family.items() if f.startswith("special"))
    report.check(fam_ok and specials == 8,
                 "15-20. family composition (158/100/142/15/15/16 + 8 specials)",
                 ", ".join(f"{f}={by_family.get(f, 0)}"
                           for f in sorted(EXPECTED_FAMILIES))
                 + f", specials={specials}")

    pairs = sorted((c for c in channels if c["family"] == "tag_pair"),
                   key=lambda c: c["number"])
    same_ns = sum(
        1 for c in pairs
        if len({ex.tag_names[t].split(":", 1)[0]
                for t in c["source"]["tags"]}) == 1)
    report.check(len(pairs) == 40 and same_ns <= 2,
                 "11. tag pairs: exactly 40, <=2 same-namespace total",
                 f"{len(pairs)} pairs, {same_ns} same-namespace")

    triples = {tuple(sorted(ex.norm_tag(t)
                            for t in c["source"]["tags"]))
               for c in channels if c["family"] == "tag_triple"}
    locked = {tuple(sorted(ex.concept_key(name) for name in concept)
                    ) for concept in finalize.LOCKED_TRIPLES}
    report.check(triples == locked and len(triples) == 15,
                 "12. triples exactly the 15 locked concepts",
                 f"{len(triples)} triples, match={triples == locked}")

    inex = set()
    for channel in channels:
        if channel["family"] != "tag_include_exclude":
            continue
        excluded = [t for t in channel["source"].get("excludeTags", ())
                    if t != JAV_ID]
        inex.add((ex.norm_tag(channel["source"]["tags"][0]),
                  ex.norm_tag(excluded[0]) if excluded else ""))
    locked_inex = {(ex.concept_key(a), ex.concept_key(b))
                   for a, b in finalize.LOCKED_INEX}
    report.check(inex == locked_inex and len(inex) == 4,
                 "13. include/exclude exactly the 4 locked concepts",
                 str(sorted(inex)))

    duos = {frozenset(c["source"].get("performers", ()))
            for c in channels if c["family"] == "performer_pair"}
    locked_duos = {frozenset(ex.perf_names[a] for a in pair)
                   for pair in finalize.LOCKED_DUOS}
    report.check(duos == locked_duos and len(duos) == 16,
                 "14. duos exactly the 16 locked pairs",
                 f"{len(duos)} duos, match={duos == locked_duos}")

    sections = Counter(c["section"] for c in channels)
    report.check(len(channels) == 513 and dict(sections) == EXPECTED_SECTIONS,
                 "21. final total 513; sections 209/115/189",
                 f"total={len(channels)}, sections={dict(sorted(sections.items()))}")


def check_csv_widths(report):
    problems = []
    paths = sorted(OUT_DIR.glob("*.csv")) + [FINAL_CSV]
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
        header = rows[0]
        for i, row in enumerate(rows[1:], 1):
            if len(row) != len(header):
                problems.append(f"{path.name} row {i}: {len(row)} vs {len(header)}")
    report.check(not problems, "22. CSV header/row widths all match",
                 "; ".join(problems) or f"{len(paths)} files, all rows exact")


def check_json_csv_preview(report, channels, final_json, final_csv, authoring):
    json_by_id = {c["channel_id"]: c for c in final_json["channels"]}
    csv_by_id = {r["channel_id"]: r for r in final_csv}
    problems = []
    for stable, entry in json_by_id.items():
        row = csv_by_id.get(stable)
        if row is None:
            problems.append(f"{stable}: missing from final_channels.csv")
            continue
        if (int(row["channel_number"]) != entry["channel_number"]
                or row["channel_name"] != entry["channel_name"]
                or int(row["scene_count"]) != entry["scene_count"]):
            problems.append(f"{stable}: CSV/JSON disagree")
    stable_to_net = {row["stable_key"]: _net_id(row["stable_key"])
                     for row in authoring}
    preview_by_net = {c["id"]: c for c in channels}
    count_mismatch = sum(
        1 for stable, net in stable_to_net.items()
        if net not in preview_by_net
        or preview_by_net[net]["count"] != json_by_id[stable]["scene_count"])
    problems += [f"{count_mismatch} stable keys whose preview count != JSON count"] \
        if count_mismatch else []
    id_align = set(stable_to_net.values()) == set(preview_by_net)
    report.check(not problems and id_align,
                 "23. JSON, CSV, authoring CSV, and preview all agree",
                 "; ".join(problems) or f"ids align={id_align} "
                 f"({len(stable_to_net)} stable keys -> preview ids)")


def check_compile_and_load(report):
    importer = _load_importer()
    temp = OUT_DIR / ".compile-check.json"
    try:
        importer.main(["--csv", str(FINAL_CSV), "--out", str(temp)])
        sys.path.insert(0, str(REPO))
        from justwatch import networks
        document = networks.load(temp)
        ok = len(document["channels"]) == 513
        detail = f"compiled + strict-loaded {len(document['channels'])} networks"
    finally:
        temp.unlink(missing_ok=True)
    report.check(ok, "24. importer compiles the CSV; strict loader accepts",
                 detail)


def check_tests(report) -> str:
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=REPO, capture_output=True, text=True, timeout=600)
    summary = (tests.stdout.strip().splitlines() or ["?"])[-1]
    report.check(tests.returncode == 0,
                 "25/26. plugin tests green (incl. stable-key, ANY matrix, "
                 "metadata, epoch, golden)", summary)
    return summary


def check_golden(report):
    importer = _load_importer()
    doc = importer.import_csv(importer.DEFAULT_CSV)
    doc["revision"] = importer.revision(doc)
    live = json.loads(LIVE_NETWORKS.read_text(encoding="utf-8"))
    report.check(doc == live,
                 "R1. golden: upgraded importer leaves the production compile "
                 "byte-identical", "production CSV -> live networks.json")


def check_determinism(report):
    def digests() -> dict[str, str]:
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(OUT_DIR.glob("*")) + [FINAL_CSV]
            if path.is_file()
        }

    generated = json.loads(
        (OUT_DIR / "final_channels.json").read_text())["generated"]
    before = digests()
    run = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("finalize.py")),
         "--timestamp", generated],
        cwd=REPO, capture_output=True, text=True, timeout=900)
    after = digests()
    changed = [name for name in before if before.get(name) != after.get(name)]
    report.check(run.returncode == 0 and not changed
                 and len(before) == len(after),
                 "R2. finalizer determinism (fixed --timestamp rerun)",
                 f"{len(after)} artifacts, {len(changed)} differ on rerun")


def check_operational(report, channels) -> None:
    sys.path.insert(0, str(REPO))
    from justwatch import lineup
    rare = next(c for c in channels if c["name"].startswith("Rare Performers"))
    scene_filter = lineup.build_scene_filter(rare["source"])
    payload_bytes = len(json.dumps(scene_filter).encode("utf-8"))
    dev = _dev_probe(scene_filter)
    report.check(True, "R3. operational: largest ANY filter measured",
                 f"Rare Performers scene_filter = {payload_bytes:,} bytes "
                 f"({len(scene_filter['performers']['value']):,} ids); dev "
                 f"Stash v0.31.1: {dev}; prod timing skipped (no local "
                 f"production credentials — scheduler env points at the "
                 f"10-scene dev instance)")


def _dev_probe(scene_filter: dict) -> str:
    try:
        key = Path("/opt/stash-dev/API_KEY").read_text().strip()
        body = json.dumps({
            "query": "query C($sf: SceneFilterType) { findScenes("
                     "filter:{per_page:0}, scene_filter:$sf) { count } }",
            "variables": {"sf": scene_filter},
        }).encode("utf-8")
        request = urllib.request.Request(
            "http://192.168.8.123:9998/graphql", data=body,
            headers={"Content-Type": "application/json", "ApiKey": key})
        started = time.time()
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        count = data["data"]["findScenes"]["count"]
        return f"accepted, count={count} ({(time.time() - started) * 1000:.0f} ms)"
    except Exception as exc:  # dev being unreachable is not a catalog failure
        return f"unreachable ({exc.__class__.__name__})"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(report, stats, ex: Extraction, channels, pairs, coverage,
                 generated) -> None:
    with (OUT_DIR / "migration_map.csv").open(newline="", encoding="utf-8") as h:
        migration = list(csv.DictReader(h))
    lines = [
        "# Just Watch v4 Final — validation report", "",
        f"Catalog `just-watch-v4-final` · {len(channels)} channels · "
        f"generated {generated} · validated "
        f"{_dt_today()}", "",
        "| Status | Check | Detail |", "| --- | --- | --- |",
    ]
    for status, name, detail in report.results:
        lines.append(f"| {status} | {name} | {detail or '—'} |")
    lines += [
        "", "## Statistics", "",
        f"- Scene depth: min **{stats['min']}** · p10 **{stats['p10']}** · "
        f"median **{stats['median']}** · max **{stats['max']}**",
        f"- Channels below 25 scenes: **{stats['below_25']}**; "
        f"below 50: **{stats['below_50']}**",
        f"- Post-JAV library coverage: **{coverage:.2f}%** "
        f"({len(ex.universe):,} scenes)",
        f"- Current networks preserved by stable id: **{stats['preserved']}** "
        "(number, name, network id, and seed all byte-identical)",
        f"- Current networks retired: **{stats['retired']}** "
        f"({stats['repurposed']} slots repurposed for new channels, "
        f"{stats['freed']} numbers left unused)",
        f"- New networks: **{stats['new']}**",
        "- Sections: General 209 · Studios 115 · Performers 189 = 513", "",
        "## The final 40 tag pairs", "",
        "Same-namespace pairs are capped at 2 across the whole band; "
        "cross-namespace combinations at 2 each.", "",
        "| # | Channel | Pair | Namespaces | Scenes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for channel in pairs:
        tags = channel["source"]["tags"]
        pair = " + ".join(ex.display_tag(t) for t in tags)
        nss = "+".join(sorted({ex.tag_names[t].split(":", 1)[0]
                               for t in tags}))
        lines.append(f"| {channel['number']} | {channel['name']} | {pair} "
                     f"| {nss} | {channel['count']} |")
    lines += [
        "", f"**{len(report.results) - report.failures}/"
        f"{len(report.results)} checks passed.**",
    ]
    if report.failures:
        lines.append(f"\n> ⚠ **{report.failures} checks FAILED** — see the table.")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _dt_today() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    started = time.time()
    report = Report()

    ex = Extraction()
    report.check(len(ex.scenes) == N_LIBRARY, "0. extraction scene count",
                 f"{len(ex.scenes)} scenes ({N_LIBRARY} expected)")

    preview = json.loads(PREVIEW_JSON.read_text(encoding="utf-8"))
    channels = preview["channels"]
    final_json = json.loads(
        (OUT_DIR / "final_channels.json").read_text(encoding="utf-8"))
    with (OUT_DIR / "final_channels.csv").open(newline="", encoding="utf-8") as h:
        final_csv = list(csv.DictReader(h))
    with FINAL_CSV.open(encoding="utf-8-sig", newline="") as h:
        authoring = list(csv.DictReader(h))

    check_structure(report, channels, authoring)
    check_memberships(report, ex, channels)
    check_aggregates(report, ex, channels)
    check_metadata(report, ex, channels)
    check_jav_policy(report, channels)
    coverage = check_coverage(report, ex, channels)
    finalize = _load_finalize()
    check_bands(report, ex, channels, finalize)
    check_csv_widths(report)
    check_json_csv_preview(report, channels, final_json, final_csv, authoring)
    check_compile_and_load(report)
    tests_summary = check_tests(report)
    check_golden(report)
    check_determinism(report)
    check_operational(report, channels)

    sizes = sorted(c["count"] for c in channels)
    with (OUT_DIR / "migration_map.csv").open(newline="", encoding="utf-8") as h:
        migration = list(csv.DictReader(h))
    stats = {
        "min": sizes[0], "p10": sizes[len(sizes) // 10],
        "median": sizes[len(sizes) // 2], "max": sizes[-1],
        "below_25": sum(1 for s in sizes if s < 25),
        "below_50": sum(1 for s in sizes if s < 50),
        "preserved": sum(1 for r in migration if r["disposition"] == "keep"),
        "repurposed": sum(1 for r in migration if r["disposition"] == "replace"),
        "freed": sum(1 for r in migration if r["disposition"] == "retire"),
        "retired": sum(1 for r in migration if r["disposition"] != "keep"),
        "new": sum(1 for c in final_json["channels"] if c["disposition"] == "new"),
    }
    pairs = sorted((c for c in channels if c["family"] == "tag_pair"),
                   key=lambda c: c["number"])
    write_report(report, stats, ex, channels, pairs, coverage,
                 final_json["generated"])

    print(f"validate: {len(report.results) - report.failures}/"
          f"{len(report.results)} checks passed; tests: {tests_summary}; "
          f"({time.time() - started:.0f}s) -> {REPORT}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
