#!/usr/bin/env python3
"""Just Watch v4 final — assemble the approved 513-channel network catalog.

Inputs (env-overridable):
  JW_V3_SCRIPTS  the v3 analysis package's scripts/ dir (jwlib.py, jwv2.py)
  JW_STAGE       the v3 package's stage-outputs/ dir (selection_v2.pkl)
  JW_EXTRACT     the stash-extract bundle (scenes.jsonl, tags.csv, ...)

Outputs:
  data/proposed_channels_final.csv                 the authoring CSV (31 columns)
  analysis/just-watch-final/                       the review artifacts
  analysis/just-watch-final/networks.preview.json  compiled via the REAL
      importer (tools/import_channels.py --csv ... --out ...); the live
      justwatch/networks.json is never touched.

Determinism: two runs with identical inputs (and the same --timestamp)
produce byte-identical artifacts. Every ranking is tie-broken by a canonical
semantic key.

Locked decisions (do not rerun):
  - keep v3's 158 performer / 100 studio / 142 tag / 15 perf+tag /
    15 studio+tag selections and the 8 approved discovery concepts verbatim
  - tag pairs: corrected same-namespace semantics (<=2 same-ns pairs TOTAL,
    <=2 per cross-ns combination, anchor cap 3, Jaccard gate 0.60,
    reservation floors SET/THEME/PROD/CAST/KINK/WARD)
  - 15 locked triples, 4 locked include/exclude, 16 locked duos
  - migration matches the CURRENT 795-row production CSV; survivors keep
    number + production name byte-exact, stable_key = "<num>|<name>"
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import json
import math
import os
import pickle
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT_DIR = REPO / "analysis" / "just-watch-final"
FINAL_CSV = REPO / "data" / "proposed_channels_final.csv"
BASELINE_CSV = REPO / "data" / "proposed_channels_new_taxonomy_scene_validated.csv"
PREVIEW_JSON = OUT_DIR / "networks.preview.json"

JW_V3_SCRIPTS = Path(os.environ.get(
    "JW_V3_SCRIPTS",
    "/home/shahram/dev/StashAppAndroidTV/analysis/just-watch-redesign/scripts"))
JW_STAGE = Path(os.environ.get(
    "JW_STAGE", str(JW_V3_SCRIPTS.parent / "stage-outputs")))
JW_EXTRACT = Path(os.environ.get("JW_EXTRACT", "/home/shahram/dev/stash-extract"))

sys.path.insert(0, str(JW_V3_SCRIPTS))
from jwv2 import V2Index  # noqa: E402  (needs JW_V3_SCRIPTS on sys.path)

JAV_ID = "9320"
N_LIBRARY = 20_299
EXTRACTION_DATE = "2026-09-08"
NEW_ARRIVALS_ANCHOR = "2026-03-12"  # extraction date - 180 days (v3 reference)

FINAL_TOTAL = 513
EXPECTED_SECTIONS = {"general": 209, "studios": 115, "performers": 189}
EXPECTED_FAMILIES = {
    "performer_spotlight": 158, "studio_spotlight": 100, "tag_spotlight": 142,
    "tag_pair": 40, "tag_triple": 15, "tag_include_exclude": 4,
    "performer_tag": 15, "studio_tag": 15, "performer_pair": 16,
    "special_one_scene": 1, "special_rare_performers": 1, "special_prolific": 1,
    "special_fringe_studios": 1, "special_era": 2, "special_duration": 1,
    "special_recent": 1,
}

# The eight approved discovery/meta concepts (Quick Fixes stays retired).
SPECIAL_CONCEPTS = {
    "special:one_scene": ("special_one_scene", "One-Scene Performers",
                          "jw:v1:special:one-scene-performers"),
    "special:rare": ("special_rare_performers", "Rare Performers (<5 Scenes)",
                     "jw:v1:special:rare-performers"),
    "special:prolific": ("special_prolific", "Prolific Performers (100+)",
                         "jw:v1:special:prolific-performers"),
    "special:fringe": ("special_fringe_studios", "Fringe Studios",
                       "jw:v1:special:fringe-studios"),
    "special:era:2010s": ("special_era", "2010s Vault",
                          "jw:v1:special:vault-2010s"),
    "special:era:2020s": ("special_era", "2020s Vault",
                          "jw:v1:special:vault-2020s"),
    "special:epic": ("special_duration", "Feature Length (60+ Min)",
                     "jw:v1:special:feature-length"),
    "special:new": ("special_recent", "New Arrivals (Last 180 Days)",
                    "jw:v1:special:new-arrivals"),
}

# Locked tag triples (canonical display names, order-insensitive).
LOCKED_TRIPLES = [
    ("Fingering", "Big dick", "Spanking/impact"),
    ("Cumshot-mouth", "Casual", "Gonzo"),
    ("Blowjob", "Roleplay", "Narrative"),
    ("1M1F", "Roleplay", "Dom/Sub"),
    ("1M1F", "Vaginal sex", "Casual"),
    ("Vaginal sex", "Big dick", "Spanking/impact"),
    ("Cunnilingus", "Bedroom", "Gonzo"),
    ("Cumshot-face", "Big breasts", "Heels"),
    ("Cunnilingus", "Big breasts", "Cheating"),
    ("Anal sex", "Shaved", "Humiliation"),
    ("Cumshot-face", "Shaved", "Lingerie"),
    ("Anal sex", "Roleplay", "Bondage"),
    ("Cunnilingus", "Big dick", "Cheating"),
    ("1M1F", "Cheating", "Gonzo"),
    ("Cumshot-face", "Dress", "Gonzo"),
]

# Locked include/exclude concepts: (include, excluded).
LOCKED_INEX = [
    ("Narrative", "Gonzo"),
    ("Roleplay", "Narrative"),
    ("Spanking/impact", "Gonzo"),
    ("Gonzo", "1M1F"),
]

# Locked performer duos (display names).
LOCKED_DUOS = [
    ("Ryan Madison", "Kelly Madison"),
    ("Martin Stein", "Nancy Ace"),
    ("Cory Chase", "Luke Longly"),
    ("Shane Diesel", "Boz"),
    ("Little Caprice", "Marcello Bravo"),
    ("Mandy Flores", "David Flores"),
    ("Beretta James", "Dylan Ryan"),
    ("Adam Ocelot", "Eva Elfie"),
    ("Iona Grace", "Sparky Sin Claire"),
    ("Dylan Ryan", "Krysta Kaos"),
    ("Nerine Mechanique", "Lilla Katt"),
    ("Francesca Le", "Mark Wood"),
    ("Steve Holmes", "Silvia Rubi"),
    ("Krysta Kaos", "Maestro Stefanos"),
    ("Iona Grace", "Lilla Katt"),
    ("Steve Holmes", "Max Cortes"),
]

DUO_MIN_SCENES = 12
DUO_MUTUALITY = 0.20
DUO_CAPTURE = 0.60
DUO_PER_PERFORMER_CAP = 2

PAIR_BAND = 40
PAIR_ANCHOR_CAP = 3
PAIR_JACCARD_GATE = 0.60
PAIR_SAME_NS_BUDGET = 2       # same-namespace pairs TOTAL across the band
PAIR_CROSS_COMBO_CAP = 2      # per unordered cross-namespace combination
PAIR_RESERVATIONS = ("SET", "THEME", "PROD", "CAST", "KINK", "WARD")

METRIC_NAMESPACES = {"BODY", "AGE", "DEMO"}
INTENT_NAMESPACES = {"ACT", "THEME", "KINK", "PROD", "CAST", "SET", "WARD"}

# Presentation buckets (report-only; the compiled tier brands by section).
NS_BUCKET = {"ACT": "Acts", "THEME": "Themes", "KINK": "Kinks & Fetish",
             "PROD": "Production Styles", "CAST": "Cast Configurations",
             "SET": "Settings", "WARD": "Wardrobe", "BODY": "Body & Attributes",
             "AGE": "Ages", "DEMO": "Ethnicity & Demo"}
FAM_BUCKET = {"special_one_scene": "Discovery & Deep Cuts",
              "special_rare_performers": "Discovery & Deep Cuts",
              "special_prolific": "Discovery & Deep Cuts",
              "special_fringe_studios": "Discovery & Deep Cuts",
              "special_era": "Discovery & Deep Cuts",
              "special_duration": "Discovery & Deep Cuts",
              "special_recent": "Discovery & Deep Cuts",
              "tag_pair": "Tag Crossovers", "tag_triple": "Tag Crossovers",
              "tag_include_exclude": "Tag Crossovers",
              "performer_spotlight": "Performers",
              "performer_pair": "Duos", "performer_tag": "Performer Specials",
              "studio_spotlight": "Studios", "studio_tag": "Studio Signatures"}
NS_GLYPH = {"ACT": "\uf03d", "THEME": "\uf0eb", "KINK": "\uf0c4",
            "PROD": "\uf030", "CAST": "\uf007", "SET": "\uf015",
            "WARD": "\uf07a", "BODY": "\uf182", "AGE": "\uf1fd",
            "DEMO": "\uf130"}
FAM_GLYPH = {"special_one_scene": "\uf005", "special_rare_performers": "\uf004",
             "special_prolific": "\uf091", "special_fringe_studios": "\uf06c",
             "special_era": "\uf1da", "special_duration": "\uf017",
             "special_recent": "\uf236", "tag_pair": "\uf0c5",
             "tag_triple": "\uf0c5", "tag_include_exclude": "\uf14e",
             "performer_spotlight": "\uf007", "performer_pair": "\uf0c5",
             "performer_tag": "\uf0a1", "studio_spotlight": "\uf091",
             "studio_tag": "\uf0d6"}
FAM_COLOR = {"special": "#FF7043", "tag_spotlight": "#455A64",
             "tag_pair": "#5C6BC0", "tag_triple": "#5C6BC0",
             "tag_include_exclude": "#7E57C2", "performer_spotlight": "#EC407A",
             "performer_pair": "#D81B60", "performer_tag": "#C2185B",
             "studio_spotlight": "#26A69A", "studio_tag": "#00897B"}

AUTHORING_COLUMNS = [
    "channel_number", "channel_name", "channel_family", "exact_scene_count",
    "library_share_pct",
    "include_tag_logic", "include_tag_ids_all", "include_tags_all",
    "exclude_tag_logic", "exclude_tag_ids_any", "exclude_tags_any",
    "include_performer_logic", "include_performer_ids_all", "include_performers_all",
    "exclude_performer_logic", "exclude_performer_ids_any", "exclude_performers_any",
    "include_studio_logic", "include_studio_ids_any", "include_studios_any",
    "exclude_studio_logic", "exclude_studio_ids_any", "exclude_studios_any",
    "filter_expression", "affinity_lift", "rationale",
    # v0.6 additions (all optional; blank keeps legacy behavior)
    "stable_key", "scene_date_from", "scene_date_to",
    "duration_min_seconds", "created_within_days",
]

FINAL_HEADER = [
    "channel_id", "channel_number", "channel_name", "proposed_name",
    "category", "channel_type", "disposition", "deployment",
    "scene_count", "library_percent", "distinct_performers", "distinct_studios",
    "filter_definition", "tag_ids", "tag_names",
    "performer_ids", "performer_names", "studio_ids", "studio_names",
    "count_changed", "quality_score", "reason_selected",
]


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    """Width-asserting CSV writer — the v3 bug was a header/row mismatch."""
    for i, row in enumerate(rows):
        if len(row) != len(header):
            raise SystemExit(
                f"{path.name}: row {i} has {len(row)} fields, header has "
                f"{len(header)}: {row!r}")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def norm(name: str) -> str:
    return name.split(":", 1)[1].strip() if ":" in name else name


def section_of(family: str) -> str:
    if family.startswith("studio"):
        return "studios"
    if family.startswith("performer"):
        return "performers"
    return "general"


def tag_display(idx: V2Index, tag_id: str) -> str:
    return idx.tag_names.get(tag_id, tag_id)


def perf_display(idx: V2Index, performer_id: str) -> str:
    return idx.perf_names.get(performer_id, performer_id)


def studio_display(idx: V2Index, studio_id: str) -> str:
    return idx.studio_names.get(studio_id, studio_id)


# ---------------------------------------------------------------------------
# Quality scoring — replicates the v3 stage-3 formulas exactly so locked
# bands report scores on the same scale as the kept v3 selections.
# ---------------------------------------------------------------------------

def _depth_entity(n: int) -> int:
    if n >= 1000: return 30
    if n >= 500: return 27
    if n >= 200: return 24
    if n >= 100: return 20
    if n >= 50: return 15
    if n >= 25: return 10
    return 4


def _depth_crossover(n: int) -> int:
    if n >= 1000: return 19
    if n >= 500: return 18
    if n >= 250: return 17
    if n >= 100: return 15
    if n >= 50: return 12
    if n >= 25: return 8
    return 4


_IDENT = {"tag_spotlight": 22, "tag_pair": 19, "tag_triple": 16,
          "tag_include_exclude": 18, "performer_spotlight": 25,
          "performer_tag": 20, "performer_pair": 22, "studio_spotlight": 25,
          "studio_tag": 20}
_CROSS_FAM = {"tag_pair", "tag_triple", "tag_include_exclude",
              "performer_tag", "studio_tag"}


def quality_score(channel: dict, idx: V2Index, tag_net_size: dict,
                  n_library: int) -> int:
    family = channel["family"]
    n = channel["size_net"]
    ident = _IDENT.get(family, 15)
    red, spec, lift = 0.0, 5, None
    if family == "tag_pair":
        a, b = channel["ids"]["tags"]
        lift = n * n_library / max(tag_net_size[a] * tag_net_size[b], 1)
        red = channel.get("capture_of_smaller_parent", 0.0)
        spec = min(10, max(0, round(4 * (lift - 1))))
    elif family == "tag_triple":
        a, b, c = channel["ids"]["tags"]
        lift = n * n_library * n_library / max(
            tag_net_size[a] * tag_net_size[b] * tag_net_size[c], 1)
        red = channel.get("max_parent_pair_capture", 0.0)
        spans = len({idx.namespace(tag_display(idx, t)) for t in (a, b, c)})
        spec = min(10, max(0, round(3 * (lift - 1))) + (2 if spans >= 3 else 0))
    elif family == "tag_include_exclude":
        red = 1.0 - channel.get("removed_share", 0.5)
        spec = min(10, round(10 * channel.get("removed_share", 0.5)))
    elif family in ("performer_tag", "studio_tag"):
        lift = channel.get("lift", 1.0)
        spec = min(10, max(0, round(4 * (lift - 1))))
        red = channel.get("share_of_performer", channel.get(
            "share_of_studio", 0.5)) * 0.5
    elif family == "performer_pair":
        spec = min(10, round(10 * channel.get("mutuality", 0.3)))
    channel["lift"] = round(lift, 2) if lift is not None else channel.get("lift")

    pen = 0
    if family in _CROSS_FAM:
        for t in channel["ids"].get("tags", []):
            ns = idx.namespace(tag_display(idx, t))
            if ns in METRIC_NAMESPACES:
                pen += 3
            if tag_net_size.get(t, 0) > 6000:
                pen += 3
            if ns in INTENT_NAMESPACES:
                pen -= 2
        if family == "performer_tag":
            nss = {idx.namespace(tag_display(idx, t))
                   for t in channel["ids"].get("tags", [])}
            if nss & {"AGE", "DEMO"} and channel.get("lift", 1) < 3.0:
                pen += 6
        pen = max(pen, -4)

    dp, ds = channel["performers"], channel["studios"]
    variety = min(20.0, round(
        10 * math.log10(1 + dp) / math.log10(1501) +
        10 * math.log10(1 + ds) / math.log10(401), 1))
    depth = _depth_crossover(n) if family in _CROSS_FAM else _depth_entity(n)
    q = depth + variety + ident + spec - round(12 * red) - pen
    return max(0, min(100, round(100 * q / 85)))


# ---------------------------------------------------------------------------
# Corrected tag-pair selection
# ---------------------------------------------------------------------------

def jaccard_cut(candidate: dict, chosen: list[dict], gate: float) -> bool:
    for other in chosen:
        inter = (candidate["bits_net"] & other["bits_net"]).bit_count()
        if not inter:
            continue
        union = (candidate["bits_net"] | other["bits_net"]).bit_count()
        if inter / union >= gate:
            return True
    return False


def select_tag_pairs(pool: list[dict], idx: V2Index) -> list[dict]:
    """The corrected v4 pair band.

    v3's ``same_ns_cap`` collapsed an ACT+ACT pair's namespace set to
    {"ACT"}, so its pairwise-count loops never ran and same-namespace pairs
    were uncapped. Here a same-namespace pair consumes one shared budget of
    ``PAIR_SAME_NS_BUDGET`` slots across the whole band, and cross-namespace
    combinations are capped per unordered combination.
    """
    ordered = sorted(pool, key=lambda c: (-c["quality_score"], -c["size_net"],
                                          c["key"]))
    chosen: list[dict] = []
    anchor: Counter = Counter()
    cross_combo: Counter = Counter()
    state = {"same_ns": 0}
    picked: set[str] = set()

    def namespaces(candidate: dict) -> list[str]:
        return [idx.namespace(tag_display(idx, t))
                for t in candidate["ids"]["tags"]]

    def try_take(candidate: dict) -> bool:
        if len(chosen) >= PAIR_BAND or candidate["key"] in picked:
            return False
        if any(anchor[t] >= PAIR_ANCHOR_CAP for t in candidate["ids"]["tags"]):
            return False
        ns = namespaces(candidate)
        if ns[0] == ns[1]:
            if state["same_ns"] >= PAIR_SAME_NS_BUDGET:
                return False
        else:
            combo = tuple(sorted(ns))
            if cross_combo[combo] >= PAIR_CROSS_COMBO_CAP:
                return False
        if jaccard_cut(candidate, chosen, PAIR_JACCARD_GATE):
            return False
        chosen.append(candidate)
        picked.add(candidate["key"])
        for t in candidate["ids"]["tags"]:
            anchor[t] += 1
        if ns[0] == ns[1]:
            state["same_ns"] += 1
        else:
            cross_combo[tuple(sorted(ns))] += 1
        return True

    for ns in PAIR_RESERVATIONS:
        for candidate in ordered:
            if ns in namespaces(candidate) and try_take(candidate):
                break
    for candidate in ordered:
        try_take(candidate)
        if len(chosen) >= PAIR_BAND:
            break
    if len(chosen) < PAIR_BAND:
        raise SystemExit(
            f"corrected pair selection found only {len(chosen)}/{PAIR_BAND} "
            f"channels; not relaxing the caps to fill the band")
    return chosen


# ---------------------------------------------------------------------------
# Locked bands (triples / include-exclude / duos)
# ---------------------------------------------------------------------------

def _concept_key(display: str) -> str:
    """Normalized form for matching locked concept names to canonical tags:
    the locked lists use compact spellings ("Cumshot-mouth") while canonical
    tags may spell the same concept "Cumshot - mouth"."""
    return "".join(ch for ch in display.lower() if ch not in " -_")


def _resolve_tag(display: str, canonical: dict) -> str:
    want = _concept_key(norm(display))
    matches = sorted((t for t, name in canonical.items()
                      if _concept_key(norm(name)) == want), key=int)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"locked concept {display!r} is not a canonical tag")
    raise SystemExit(f"locked concept {display!r} is ambiguous: {matches}")


def locked_triples(idx: V2Index) -> list[dict]:
    canonical = idx.canonical_tags()
    net_bits = {t: idx.net(idx.bits(idx.tag_scenes[t])) for t in canonical}
    out = []
    for concept in LOCKED_TRIPLES:
        ids = tuple(sorted((_resolve_tag(n, canonical) for n in concept), key=int))
        bits = net_bits[ids[0]] & net_bits[ids[1]] & net_bits[ids[2]]
        n = idx.size(bits)
        if n <= 0:
            raise SystemExit(f"locked triple {concept} has no scenes")
        performers, studios = idx.stats(bits)
        out.append({
            "key": "tagtriple:" + "+".join(ids), "family": "tag_triple",
            "name": " + ".join(norm(tag_display(idx, t)) for t in ids),
            "bits_net": bits, "size_net": n,
            "performers": performers, "studios": studios,
            "ids": {"tags": list(ids)},
            "excludes": {"tags": [JAV_ID]},
            "max_parent_pair_capture": round(max(
                idx.capture(bits, net_bits[ids[i]] & net_bits[ids[j]])
                for i in range(3) for j in range(i + 1, 3)), 3),
        })
    return out


def locked_include_exclude(idx: V2Index) -> list[dict]:
    canonical = idx.canonical_tags()
    net_bits = {t: idx.net(idx.bits(idx.tag_scenes[t])) for t in canonical}
    out = []
    for include, exclude in LOCKED_INEX:
        x = _resolve_tag(include, canonical)
        y = _resolve_tag(exclude, canonical)
        bits = net_bits[x] & ~net_bits[y]
        n = idx.size(bits)
        if n <= 0:
            raise SystemExit(f"locked channel '{include} without {exclude}' "
                             f"has no scenes")
        share = idx.size(net_bits[x] & net_bits[y]) / max(idx.size(net_bits[x]), 1)
        performers, studios = idx.stats(bits)
        out.append({
            "key": f"tagex:{x}-{y}", "family": "tag_include_exclude",
            "name": f"{norm(tag_display(idx, x))} (no {norm(tag_display(idx, y))})",
            "bits_net": bits, "size_net": n,
            "performers": performers, "studios": studios,
            "ids": {"tags": [x]},
            "excludes": {"tags": sorted({JAV_ID, y})},
            "excluded_name": norm(tag_display(idx, y)),
            "removed_share": round(share, 3),
        })
    return out


def locked_duos(idx: V2Index) -> list[dict]:
    name_to_ids = defaultdict(list)
    for pid, pname in idx.perf_names.items():
        name_to_ids[pname].append(pid)
    out = []
    per_performer: Counter = Counter()
    for a_name, b_name in LOCKED_DUOS:
        for name in (a_name, b_name):
            if len(name_to_ids.get(name, [])) != 1:
                raise SystemExit(f"duo performer {name!r} does not resolve "
                                 f"uniquely: {name_to_ids.get(name)}")
        a, b = sorted((name_to_ids[a_name][0], name_to_ids[b_name][0]), key=int)
        net_a = idx.net(idx.bits(idx.perf_scenes[a]))
        net_b = idx.net(idx.bits(idx.perf_scenes[b]))
        bits = net_a & net_b
        pair_n, na, nb = idx.size(bits), idx.size(net_a), idx.size(net_b)
        mutuality = min(pair_n / max(na, 1), pair_n / max(nb, 1))
        capture = pair_n / max(min(na, nb), 1)
        if per_performer[a] >= DUO_PER_PERFORMER_CAP or \
                per_performer[b] >= DUO_PER_PERFORMER_CAP:
            raise SystemExit(f"duo {a_name} & {b_name} exceeds the "
                             f"{DUO_PER_PERFORMER_CAP}-per-performer cap")
        if not (pair_n >= DUO_MIN_SCENES and
                (mutuality >= DUO_MUTUALITY or capture >= DUO_CAPTURE)):
            raise SystemExit(
                f"duo {a_name} & {b_name} fails the approved rule "
                f"(pair={pair_n}, mutuality={mutuality:.3f}, capture={capture:.3f})")
        per_performer[a] += 1
        per_performer[b] += 1
        performers, studios = idx.stats(bits)
        out.append({
            "key": f"perfpair:{a}+{b}", "family": "performer_pair",
            "name": f"{perf_display(idx, a)} & {perf_display(idx, b)}",
            "bits_net": bits, "size_net": pair_n,
            "performers": performers, "studios": studios,
            "ids": {"performers": [a, b]},
            "excludes": {"tags": [JAV_ID]},
            "mutuality": round(mutuality, 3), "capture": round(capture, 3),
            "a_scenes": na, "b_scenes": nb,
        })
    return out


# ---------------------------------------------------------------------------
# Discovery/meta networks (aggregates + metadata filters)
# ---------------------------------------------------------------------------

def special_channels(idx: V2Index, selection: dict) -> list[dict]:
    """The eight discovery/meta networks as real, first-class filters.

    Aggregates are MATERIALIZED entity-set snapshots of the extraction (they
    refresh when this pipeline re-runs); only New Arrivals is dynamic at
    runtime (created_within_days resolves its cutoff at query time).
    Rarity/prolificacy is defined over network-eligible post-JAV scenes, not
    raw library cardinality. The global JAV exclusion applies to all eight.
    """
    references = {c["key"]: c for c in selection["cands"]
                  if c["family"].startswith("special")}
    perf_net = {p: idx.size(idx.net(idx.bits(rows)))
                for p, rows in idx.perf_scenes.items()}

    def union_bits(ids: list[str]) -> int:
        bits = 0
        for p in ids:
            bits |= idx.net(idx.bits(idx.perf_scenes[p]))
        return bits

    one_ids = sorted((p for p, n in perf_net.items() if n == 1), key=int)
    rare_ids = sorted((p for p, n in perf_net.items() if 1 <= n < 5), key=int)
    prolific_ids = sorted((p for p, n in perf_net.items() if n >= 100), key=int)

    hier_net = {s: idx.size(idx.net(idx.hier_bits(s))) for s in idx.studio_names}
    small_studios = {s for s, n in hier_net.items() if 0 < n < 10}

    def ancestors(studio: str) -> set[str]:
        out, seen = set(), set()
        parent = idx.studio_parent.get(studio)
        while parent and parent not in seen:
            seen.add(parent)
            out.add(parent)
            parent = idx.studio_parent.get(parent)
        return out

    # Prune to maximal studios: a studio whose ancestor is also fringe is
    # fully covered by that ancestor's INCLUDES depth -1 subtree; zero-pool
    # studios contribute nothing. The union must stay bit-identical.
    fringe_ids = sorted(
        (s for s in small_studios if not (ancestors(s) & small_studios)), key=int)
    fringe_union = 0
    for s in small_studios:
        fringe_union |= idx.net(idx.hier_bits(s))
    pruned_union = 0
    for s in fringe_ids:
        pruned_union |= idx.net(idx.hier_bits(s))
    if pruned_union != fringe_union:
        raise SystemExit("fringe studio pruning changed the union")

    def era_bits(lo: str, hi: str) -> int:
        return idx.net(idx.bits({
            i for i, meta in enumerate(idx.scene_meta)
            if lo <= meta[0][:4] <= hi}))

    def finalize(key: str, bits: int, **extra) -> dict:
        family, name, stable_key = SPECIAL_CONCEPTS[key]
        reference = references.get(key)
        n = idx.size(bits)
        if reference is not None and reference["size_net"] != n:
            raise SystemExit(f"{key}: recomputed {n} scenes != v3 reference "
                             f"{reference['size_net']}")
        performers, studios = idx.stats(bits)
        channel = {
            "key": key, "family": family, "name": name,
            "stable_key": stable_key,
            "bits_net": bits, "size_net": n,
            "performers": performers, "studios": studios,
            "ids": {}, "excludes": {"tags": [JAV_ID]},
            "quality_score": reference["quality_score"] if reference else "",
        }
        channel.update(extra)
        return channel

    return [
        finalize("special:one_scene", union_bits(one_ids),
                 ids={"performers": one_ids}, criterion="performersAny",
                 note=f"ANY of {len(one_ids):,} performers with exactly 1 "
                      f"post-JAV scene (materialized snapshot)"),
        finalize("special:rare", union_bits(rare_ids),
                 ids={"performers": rare_ids}, criterion="performersAny",
                 note=f"ANY of {len(rare_ids):,} performers with 1-4 post-JAV "
                      f"scenes (materialized snapshot; zero-post-JAV "
                      f"performers excluded)"),
        finalize("special:prolific", union_bits(prolific_ids),
                 ids={"performers": prolific_ids}, criterion="performersAny",
                 note=f"ANY of {len(prolific_ids):,} performers with 100+ "
                      f"post-JAV scenes (materialized snapshot)"),
        finalize("special:fringe", pruned_union,
                 ids={"studios": fringe_ids}, criterion="studiosAny",
                 note=f"ANY of {len(fringe_ids):,} maximal studios/trees with "
                      f"fewer than 10 hierarchical post-JAV scenes "
                      f"(materialized snapshot, pruned to maximal nodes)"),
        finalize("special:era:2010s", era_bits("2010", "2019"),
                 date={"from": "2010-01-01", "to": "2019-12-31"},
                 note="scenes dated 2010-01-01 through 2019-12-31"),
        finalize("special:era:2020s", era_bits("2020", "2026"),
                 date={"from": "2020-01-01", "to": "2026-12-31"},
                 note="scenes dated 2020-01-01 through 2026-12-31 (analysis "
                      "semantics preserved; the fixed upper bound needs "
                      "annual regeneration)"),
        finalize("special:epic", idx.net(idx.bits(
                     {i for i, meta in enumerate(idx.scene_meta)
                      if meta[1] >= 3600})),
                 duration={"min": 3600},
                 note="scenes with duration >= 3600 seconds (inclusive)"),
        finalize("special:new", idx.net(idx.bits(
                     {i for i, meta in enumerate(idx.scene_meta)
                      if meta[2] >= NEW_ARRIVALS_ANCHOR})),
                 createdAt={"withinDays": 180},
                 note=f"created_at within 180 calendar days of query time "
                      f"(dynamic; generation-time reference anchor "
                      f"{NEW_ARRIVALS_ANCHOR})"),
    ]


# ---------------------------------------------------------------------------
# Assembly + migration
# ---------------------------------------------------------------------------

def build_channels(idx: V2Index, selection: dict) -> list[dict]:
    kept = [dict(c) for c in selection["selected"]
            if c["family"] in ("performer_spotlight", "studio_spotlight",
                               "tag_spotlight", "performer_tag", "studio_tag")]
    channels = kept
    channels.extend(select_tag_pairs(
        [c for c in selection["cands"] if c["family"] == "tag_pair"], idx))
    channels.extend(locked_triples(idx))
    channels.extend(locked_include_exclude(idx))
    channels.extend(locked_duos(idx))
    channels.extend(special_channels(idx, selection))
    return channels


def _split_ids(raw: str) -> list[str]:
    return sorted((x for x in (raw or "").split("|") if x), key=int)


def old_key(row: dict) -> tuple:
    family = row["channel_family"]
    if family == "performer_spotlight":
        return ("perf", "|".join(_split_ids(row["include_performer_ids_all"])))
    if family == "studio_spotlight":
        return ("studio", "|".join(_split_ids(row["include_studio_ids_any"])))
    if family == "tag_spotlight":
        return ("tag", "|".join(_split_ids(row["include_tag_ids_all"])))
    if family == "tag_pair":
        return ("tagpair", "|".join(_split_ids(row["include_tag_ids_all"])))
    if family == "tag_triple":
        return ("tagtriple", "|".join(_split_ids(row["include_tag_ids_all"])))
    if family == "tag_include_exclude":
        ex = [t for t in _split_ids(row["exclude_tag_ids_any"]) if t != JAV_ID]
        return ("tagex", "|".join(_split_ids(row["include_tag_ids_all"])),
                ex[0] if ex else "")
    if family == "performer_tag":
        return ("perftag", "|".join(_split_ids(row["include_performer_ids_all"])),
                "|".join(_split_ids(row["include_tag_ids_all"])))
    if family == "studio_tag":
        return ("studiotag", "|".join(_split_ids(row["include_studio_ids_any"])),
                "|".join(_split_ids(row["include_tag_ids_all"])))
    if family == "performer_pair":
        return ("perfpair", "|".join(_split_ids(row["include_performer_ids_all"])))
    return ("other", row["channel_number"])


def new_key(channel: dict) -> tuple:
    ids = channel["ids"]
    family = channel["family"]
    if family == "performer_spotlight":
        return ("perf", "|".join(sorted(ids["performers"], key=int)))
    if family == "studio_spotlight":
        return ("studio", "|".join(sorted(ids["studios"], key=int)))
    if family == "tag_spotlight":
        return ("tag", "|".join(sorted(ids["tags"], key=int)))
    if family == "tag_pair":
        return ("tagpair", "|".join(sorted(ids["tags"], key=int)))
    if family == "tag_triple":
        return ("tagtriple", "|".join(sorted(ids["tags"], key=int)))
    if family == "tag_include_exclude":
        ex = [t for t in channel["excludes"]["tags"] if t != JAV_ID]
        return ("tagex", "|".join(sorted(ids["tags"], key=int)),
                ex[0] if ex else "")
    if family == "performer_tag":
        return ("perftag", "|".join(sorted(ids["performers"], key=int)),
                "|".join(sorted(ids["tags"], key=int)))
    if family == "studio_tag":
        return ("studiotag", "|".join(sorted(ids["studios"], key=int)),
                "|".join(sorted(ids["tags"], key=int)))
    if family == "performer_pair":
        return ("perfpair", "|".join(sorted(ids["performers"], key=int)))
    return ("special", channel["key"])


def semantic_stable_key(channel: dict) -> str:
    ids = channel["ids"]
    family = channel["family"]
    if family == "tag_spotlight":
        return f"jw:v1:tag:{ids['tags'][0]}"
    if family == "performer_spotlight":
        return f"jw:v1:perf:{ids['performers'][0]}"
    if family == "studio_spotlight":
        return f"jw:v1:studio:{ids['studios'][0]}"
    if family == "tag_pair":
        return "jw:v1:tagpair:" + ":".join(sorted(ids["tags"], key=int))
    if family == "tag_triple":
        return "jw:v1:tagtriple:" + ":".join(sorted(ids["tags"], key=int))
    if family == "tag_include_exclude":
        ex = [t for t in channel["excludes"]["tags"] if t != JAV_ID]
        return f"jw:v1:taginex:{ids['tags'][0]}:ex{ex[0] if ex else ''}"
    if family == "performer_tag":
        return f"jw:v1:perftag:{ids['performers'][0]}:{ids['tags'][0]}"
    if family == "studio_tag":
        return f"jw:v1:studiotag:{ids['studios'][0]}:{ids['tags'][0]}"
    if family == "performer_pair":
        return "jw:v1:perfduo:" + ":".join(sorted(ids["performers"], key=int))
    return channel["stable_key"]  # specials carry their concept key


def read_baseline() -> dict[str, dict]:
    with BASELINE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 795:
        raise SystemExit(f"baseline CSV has {len(rows)} rows, expected 795")
    return {row["channel_number"]: row for row in rows}


def migrate(channels: list[dict]) -> list[dict]:
    """Assign numbers/names/stable keys; mark dispositions.

    Survivors keep their production channel_number AND channel_name
    byte-exact; their stable_key is "<number>|<production name>", which
    reproduces the legacy id/seed byte-for-byte. New channels take the
    lowest freed numbers; no survivor is renumbered.
    """
    baseline = read_baseline()
    by_key: dict[tuple, dict] = {}
    for number in sorted(baseline, key=int):
        by_key.setdefault(old_key(baseline[number]), baseline[number])

    used = {int(by_key[new_key(c)]["channel_number"])
            for c in channels if new_key(c) in by_key}
    free_numbers = iter(sorted(
        int(n) for n in baseline if int(n) not in used))
    next_fallback = max(int(n) for n in baseline) + 1

    # New channels fill freed slots in catalog order (general, then studios,
    # then performers; by concept key) so the assignment is deterministic.
    order = {"general": 0, "studios": 1, "performers": 2}
    newcomers = sorted((c for c in channels if new_key(c) not in by_key),
                       key=lambda c: (order[section_of(c["family"])], c["key"]))

    def take_number() -> int:
        nonlocal next_fallback
        number = next(free_numbers, None)
        if number is None:
            number = next_fallback
            next_fallback += 1
        return number

    assigned = [take_number() for _ in newcomers]
    for channel in channels:
        match = by_key.get(new_key(channel))
        if match is not None:
            channel["number"] = int(match["channel_number"])
            channel["proposed_name"] = channel["name"]
            channel["name"] = match["channel_name"]      # byte-exact survival
            channel["disposition"] = "keep"
            channel["stable_key"] = (f"{match['channel_number']}"
                                     f"|{match['channel_name']}")
            channel["baseline"] = match
        else:
            channel["disposition"] = "new"
            channel["number"] = None
            channel["stable_key"] = semantic_stable_key(channel)
    numbers = iter(sorted(assigned))
    for channel in sorted(channels, key=lambda c: (
            order[section_of(c["family"])], c["key"])):
        if channel["number"] is None:
            channel["number"] = next(numbers)
    channels.sort(key=lambda c: c["number"])
    return channels


# ---------------------------------------------------------------------------
# Reasons + artifact emission
# ---------------------------------------------------------------------------

def reason_for(channel: dict, idx: V2Index) -> str:
    family = channel["family"]
    n = channel["size_net"]
    if family == "tag_spotlight":
        return (f"canonical {idx.namespace(tag_display(idx, channel['ids']['tags'][0]))} "
                f"tag, {n} scenes")
    if family == "tag_pair":
        return (f"two-tag concept, captures "
                f"{round(100 * channel.get('capture_of_smaller_parent', 0))}% "
                f"of smaller parent, lift {channel.get('lift')}")
    if family == "tag_triple":
        return (f"locked three-tag micro-channel, <= "
                f"{round(100 * channel.get('max_parent_pair_capture', 0))}% of "
                f"any parent pair, {n} scenes")
    if family == "tag_include_exclude":
        return (f"locked subtraction channel: "
                f"{tag_display(idx, channel['ids']['tags'][0])} minus "
                f"{channel.get('excluded_name')}")
    if family == "performer_spotlight":
        if channel.get("jav_carried"):
            return "sanctioned JAV-only performer channel (no JAV exclusion)"
        return f"performer with 50+ post-JAV scenes ({n})"
    if family == "performer_pair":
        return (f"locked recurring duo, {n} scenes together, mutuality "
                f"{channel.get('mutuality')}, capture {channel.get('capture')}")
    if family == "performer_tag":
        return (f"specialization of {perf_display(idx, channel['ids']['performers'][0])} "
                f"({round(100 * channel.get('share_of_performer', 0))}% of their "
                f"scenes), lift {channel.get('lift')}")
    if family == "studio_spotlight":
        if channel.get("jav_exception"):
            return "sanctioned all-JAV studio (owner policy; no JAV exclusion)"
        return f"hierarchical network/studio, {n} post-JAV scenes"
    if family == "studio_tag":
        return (f"{studio_display(idx, channel['ids']['studios'][0])} signature "
                f"({round(100 * channel.get('share_of_studio', 0))}% of the "
                f"network), lift {channel.get('lift')}")
    return channel.get("note", "discovery/meta channel")


def filter_definition(channel: dict) -> dict:
    include: dict = {}
    if channel["ids"].get("tags"):
        include["tags_all"] = sorted(channel["ids"]["tags"], key=int)
    if channel["ids"].get("performers"):
        if channel.get("criterion") == "performersAny":
            include["performers_any"] = sorted(channel["ids"]["performers"], key=int)
        else:
            include["performers_all"] = sorted(channel["ids"]["performers"], key=int)
    if channel["ids"].get("studios"):
        if channel.get("criterion") == "studiosAny":
            include["studios_any_union"] = sorted(channel["ids"]["studios"], key=int)
        else:
            include["studios_any"] = sorted(channel["ids"]["studios"], key=int)
    if channel.get("date"):
        include["scene_date"] = channel["date"]
    if channel.get("duration"):
        include["duration_min_seconds"] = channel["duration"]["min"]
    if channel.get("createdAt"):
        include["created_within_days"] = channel["createdAt"]["withinDays"]
    return {"include": include,
            "exclude": {"tags_any": channel["excludes"].get("tags", [])}}


def authoring_row(channel: dict, idx: V2Index) -> list:
    tag_ids = sorted(channel["ids"].get("tags", []), key=int)
    performer_ids = sorted(channel["ids"].get("performers", []), key=int)
    studio_ids = sorted(channel["ids"].get("studios", []), key=int)
    exclude_ids = sorted(channel["excludes"].get("tags", []), key=int)
    baseline = channel.get("baseline")
    performer_logic = ("ANY" if channel.get("criterion") == "performersAny"
                       else "ALL") if performer_ids else ""
    studio_logic = "ANY" if studio_ids else ""
    return [
        channel["number"],
        channel["name"],
        channel["family"],
        channel["size_net"],
        f"{round(100 * channel['size_net'] / N_LIBRARY, 3):.3f}",
        "ALL" if tag_ids else "",
        "|".join(tag_ids),
        "|".join(tag_display(idx, t) for t in tag_ids),
        "ANY" if exclude_ids else "",
        "|".join(exclude_ids),
        "|".join("JAV" if t == JAV_ID else tag_display(idx, t)
                 for t in exclude_ids),
        performer_logic,
        "|".join(performer_ids),
        "|".join(perf_display(idx, p) for p in performer_ids),
        "", "", "",                   # exclude-performer columns (unused)
        studio_logic,
        "|".join(studio_ids),
        "|".join(studio_display(idx, s) for s in studio_ids),
        "", "", "",                   # exclude-studio columns (unused)
        baseline.get("filter_expression", "") if baseline else "",
        baseline.get("affinity_lift", "") if baseline else "",
        channel["reason"],
        channel["stable_key"],
        channel["date"]["from"] if channel.get("date") else "",
        channel["date"]["to"] if channel.get("date") else "",
        channel["duration"]["min"] if channel.get("duration") else "",
        channel["createdAt"]["withinDays"] if channel.get("createdAt") else "",
    ]


def emit(channels: list[dict], idx: V2Index, timestamp: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = read_baseline()

    class TSize(dict):
        """Memoized net tag sizes (dict-like, computes on miss)."""
        def __missing__(self, t: str) -> int:
            size = idx.size(idx.net(idx.bits(idx.tag_scenes[t])))
            self[t] = size
            return size

    tag_net_size = TSize()

    for channel in channels:
        if not str(channel.get("quality_score", "")).isdigit():
            channel["quality_score"] = quality_score(channel, idx, tag_net_size,
                                                     N_LIBRARY)
        channel["reason"] = reason_for(channel, idx)

    # ---- authoring CSV -----------------------------------------------------
    write_csv(FINAL_CSV, AUTHORING_COLUMNS,
              [authoring_row(c, idx) for c in channels])

    # ---- final_channels.csv / .json ----------------------------------------
    rows, entries = [], []
    for channel in channels:
        old = channel.get("baseline")
        count_changed = ""
        if old is not None:
            try:
                count_changed = ("yes" if int(old["exact_scene_count"])
                                 != channel["size_net"] else "no")
            except (ValueError, TypeError):
                count_changed = ""
        fd = filter_definition(channel)
        tag_ids = sorted(channel["ids"].get("tags", []), key=int)
        performer_ids = sorted(channel["ids"].get("performers", []), key=int)
        studio_ids = sorted(channel["ids"].get("studios", []), key=int)
        common = [
            channel["stable_key"], channel["number"], channel["name"],
            channel.get("proposed_name", channel["name"]),
            _bucket(channel, idx), channel["family"], channel["disposition"],
            "import-ready",
            channel["size_net"], round(100 * channel["size_net"] / N_LIBRARY, 2),
            channel["performers"], channel["studios"],
            json.dumps(fd, separators=(",", ":")),
            "|".join(tag_ids),
            "|".join(tag_display(idx, t) for t in tag_ids),
            "|".join(performer_ids),
            "|".join(perf_display(idx, p) for p in performer_ids),
            "|".join(studio_ids),
            "|".join(studio_display(idx, s) for s in studio_ids),
            count_changed, channel["quality_score"], channel["reason"],
        ]
        rows.append(common)
        entries.append({
            "channel_id": channel["stable_key"],
            "channel_number": channel["number"],
            "channel_name": channel["name"],
            "proposed_name": channel.get("proposed_name", channel["name"]),
            "disposition": channel["disposition"],
            "deployment": "import-ready",
            "section": section_of(channel["family"]),
            "category": _bucket(channel, idx),
            "channel_type": channel["family"],
            "glyph": _glyph(channel, idx), "color": _color(channel),
            "scene_count": channel["size_net"],
            "library_percent": round(100 * channel["size_net"] / N_LIBRARY, 2),
            "distinct_performers": channel["performers"],
            "distinct_studios": channel["studios"],
            "filter": fd,
            "quality_score": channel["quality_score"],
            "reason": channel["reason"],
            "jav_exception": bool(channel.get("jav_exception")
                                  or channel.get("jav_carried")),
            "count_changed": count_changed,
            "note": channel.get("note", ""),
        })
    write_csv(OUT_DIR / "final_channels.csv", FINAL_HEADER, rows)
    section_counts = Counter(section_of(c["family"]) for c in channels)
    with (OUT_DIR / "final_channels.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "catalog": "just-watch-v4-final",
            "generated": timestamp,
            "baseline": {
                "file": "data/proposed_channels_new_taxonomy_scene_validated.csv",
                "rows": len(baseline),
            },
            "extraction": {"scenes": N_LIBRARY, "generated": EXTRACTION_DATE},
            "channel_count": len(entries),
            "sections": dict(sorted(section_counts.items())),
            "analysis": {
                "rules": "v3 corrected selection + v4 locked bands "
                         "(pairs re-selected with explicit same-namespace "
                         "semantics; triples/include-exclude/duos locked)",
                "version": "v4-final",
            },
            "stable_key_contract": {
                "version": 1,
                "legacy": 'id = net_+sha1("<number>|<name>")[:8]; '
                          'seed = int(sha1("seed|<number>|<name>")[:8],16) % 2^31',
                "stable": "id basis = stable_key; seed basis = "
                          "'seed|'+stable_key; survivors seed it "
                          "'<old number>|<old production name>'; immutable "
                          "once in production",
            },
            "channels": entries,
        }, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    emit_band_artifacts(channels, idx)
    emit_migration_map(channels, baseline, idx)


def _bucket(channel: dict, idx: V2Index) -> str:
    bucket = FAM_BUCKET.get(channel["family"])
    if bucket is None and channel["family"] == "tag_spotlight":
        bucket = NS_BUCKET.get(idx.namespace(
            tag_display(idx, channel["ids"]["tags"][0])), "Tags")
    return bucket or "Other"


def _glyph(channel: dict, idx: V2Index) -> str:
    if channel["family"] == "tag_spotlight":
        return NS_GLYPH.get(idx.namespace(
            tag_display(idx, channel["ids"]["tags"][0])), "\uf111")
    return FAM_GLYPH.get(channel["family"], "\uf111")


def _color(channel: dict) -> str:
    for key, value in FAM_COLOR.items():
        if channel["family"] == key or (
                key == "special" and channel["family"].startswith("special")):
            return value
    return "#455A64"


def emit_band_artifacts(channels: list[dict], idx: V2Index) -> None:
    def sorted_ids(channel: dict, kind: str) -> list[str]:
        return sorted(channel["ids"].get(kind, []), key=int)

    pairs = sorted((c for c in channels if c["family"] == "tag_pair"),
                   key=lambda c: c["number"])
    write_csv(OUT_DIR / "final_tag_pairs.csv", [
        "channel_number", "channel_name", "tag_a", "tag_b", "tag_a_id",
        "tag_b_id", "namespace_a", "namespace_b", "same_namespace",
        "scene_count", "quality_score", "lift", "capture_of_smaller_parent",
        "stable_key"], [[
        c["number"], c["name"],
        norm(tag_display(idx, sorted_ids(c, "tags")[0])),
        norm(tag_display(idx, sorted_ids(c, "tags")[1])),
        sorted_ids(c, "tags")[0], sorted_ids(c, "tags")[1],
        idx.namespace(tag_display(idx, sorted_ids(c, "tags")[0])),
        idx.namespace(tag_display(idx, sorted_ids(c, "tags")[1])),
        "yes" if len({idx.namespace(tag_display(idx, t))
                      for t in c["ids"]["tags"]}) == 1 else "no",
        c["size_net"], c["quality_score"], c.get("lift"),
        c.get("capture_of_smaller_parent"), c["stable_key"],
    ] for c in pairs])

    triples = sorted((c for c in channels if c["family"] == "tag_triple"),
                     key=lambda c: c["number"])
    write_csv(OUT_DIR / "final_tag_triples.csv", [
        "channel_number", "channel_name", "tag_ids", "tag_names", "namespaces",
        "scene_count", "max_parent_pair_capture", "quality_score",
        "stable_key"], [[
        c["number"], c["name"],
        "|".join(sorted_ids(c, "tags")),
        "|".join(tag_display(idx, t) for t in sorted_ids(c, "tags")),
        "|".join(idx.namespace(tag_display(idx, t))
                 for t in sorted_ids(c, "tags")),
        c["size_net"], c.get("max_parent_pair_capture"),
        c["quality_score"], c["stable_key"],
    ] for c in triples])

    duos = sorted((c for c in channels if c["family"] == "performer_pair"),
                  key=lambda c: c["number"])
    write_csv(OUT_DIR / "final_performer_duos.csv", [
        "channel_number", "channel_name", "performer_ids", "a_scenes",
        "b_scenes", "scenes_together", "mutuality", "smaller_capture",
        "passes_rule", "quality_score", "stable_key"], [[
        c["number"], c["name"], "|".join(sorted_ids(c, "performers")),
        c.get("a_scenes"), c.get("b_scenes"), c["size_net"],
        c.get("mutuality"), c.get("capture"),
        "yes" if (c["size_net"] >= DUO_MIN_SCENES and
                  (c.get("mutuality", 0) >= DUO_MUTUALITY or
                   c.get("capture", 0) >= DUO_CAPTURE)) else "no",
        c["quality_score"], c["stable_key"],
    ] for c in duos])

    specials = sorted((c for c in channels if c["family"].startswith("special")),
                      key=lambda c: c["number"])
    write_csv(OUT_DIR / "final_special_channels.csv", [
        "channel_number", "channel_name", "kind", "criterion", "entity_count",
        "scene_count", "dynamic", "note", "stable_key"], [[
        c["number"], c["name"], c["family"], c.get("criterion", ""),
        len(c["ids"].get("performers", c["ids"].get("studios", []))) or "",
        c["size_net"], "yes" if c.get("createdAt") else "no",
        c.get("note", ""), c["stable_key"],
    ] for c in specials])


def emit_migration_map(channels: list[dict], baseline: dict[str, dict],
                       idx: V2Index) -> None:
    survivors = {c["number"]: c for c in channels if c["disposition"] == "keep"}
    newcomers = {c["number"]: c for c in channels if c["disposition"] == "new"}
    rows = []
    for number in sorted(baseline, key=int):
        row = baseline[number]
        survivor = survivors.get(int(number))
        replacement = newcomers.get(int(number))
        if survivor is not None:
            try:
                changed = ("yes" if int(row["exact_scene_count"])
                           != survivor["size_net"] else "no")
            except (ValueError, TypeError):
                changed = ""
            rows.append([
                number, row["channel_name"], row["channel_family"], "keep",
                number, survivor["name"], survivor["stable_key"],
                row["exact_scene_count"], survivor["size_net"], changed,
                "concept survives; number, name, and identity preserved",
            ])
        elif replacement is not None:
            rows.append([
                number, row["channel_name"], row["channel_family"], "replace",
                number, replacement["name"], replacement["stable_key"],
                row["exact_scene_count"], replacement["size_net"], "",
                f"slot repurposed: {replacement['reason']}",
            ])
        else:
            rows.append([
                number, row["channel_name"], row["channel_family"], "retire",
                "", "", "", row["exact_scene_count"], "", "",
                retirement_note(row, channels, idx),
            ])
    write_csv(OUT_DIR / "migration_map.csv", [
        "current_number", "current_name", "current_family", "disposition",
        "final_number", "final_name", "final_channel_key", "old_count",
        "final_count", "count_changed", "migration_note"], rows)
    retired = [r for r in rows if r[3] != "keep"]
    write_csv(OUT_DIR / "retired_channels.csv", [
        "current_number", "current_name", "current_family", "disposition",
        "final_name", "final_channel_key", "old_count", "note"],
        [[r[0], r[1], r[2], r[3], r[5], r[6], r[7], r[10]] for r in retired])


def retirement_note(row: dict, channels: list[dict], idx: V2Index) -> str:
    if row["channel_number"] == "835":
        survivor = next((c for c in channels
                         if c["family"] == "studio_spotlight"
                         and c["ids"].get("studios") == ["819"]), None)
        where = f" (studio concept survives at #{survivor['number']})" \
            if survivor else ""
        return ("duplicate Madonna identity retired; the unique "
                f"studio/network concept is preserved{where}")
    if row["channel_family"] == "studio_spotlight":
        survivor_studios = {c["ids"]["studios"][0] for c in channels
                            if c["family"] == "studio_spotlight"}
        ancestor = idx.studio_parent.get(row["include_studio_ids_any"])
        while ancestor and ancestor not in survivor_studios:
            ancestor = idx.studio_parent.get(ancestor)
        if ancestor and ancestor != row["include_studio_ids_any"]:
            return (f"folded into the hierarchical channel for studio "
                    f"{ancestor} ({studio_display(idx, ancestor)})")
    return "not selected in the v4 final catalog"


# ---------------------------------------------------------------------------
# Preview compile + structural sanity
# ---------------------------------------------------------------------------

def compile_preview() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "import_channels", REPO / "tools" / "import_channels.py")
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    importer.main(["--csv", str(FINAL_CSV), "--out", str(PREVIEW_JSON)])


def sanity(channels: list[dict], idx: V2Index) -> None:
    families = Counter(c["family"] for c in channels)
    if dict(families) != EXPECTED_FAMILIES:
        raise SystemExit(f"family composition mismatch:\n  got      "
                         f"{dict(sorted(families.items()))}\n  expected "
                         f"{EXPECTED_FAMILIES}")
    sections = Counter(section_of(c["family"]) for c in channels)
    if dict(sections) != EXPECTED_SECTIONS:
        raise SystemExit(f"section totals mismatch: {dict(sections)}")
    if len(channels) != FINAL_TOTAL:
        raise SystemExit(f"{len(channels)} channels, expected {FINAL_TOTAL}")
    numbers = [c["number"] for c in channels]
    keys = [c["stable_key"] for c in channels]
    ids = ["net_" + hashlib.sha1(c["stable_key"].encode()).hexdigest()[:8]
           for c in channels]
    if len(set(numbers)) != len(numbers):
        raise SystemExit("duplicate channel numbers")
    if len(set(keys)) != len(keys):
        raise SystemExit("duplicate stable keys")
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate network ids")
    pairs = [c for c in channels if c["family"] == "tag_pair"]
    same_ns = [c for c in pairs
               if len({idx.namespace(tag_display(idx, t))
                       for t in c["ids"]["tags"]}) == 1]
    if len(same_ns) > PAIR_SAME_NS_BUDGET:
        raise SystemExit(f"{len(same_ns)} same-namespace pairs, budget is "
                         f"{PAIR_SAME_NS_BUDGET}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timestamp", default=None,
                        help="generation timestamp (determinism runs pass a "
                             "fixed value)")
    args = parser.parse_args()
    timestamp = args.timestamp or _dt.datetime.now(
        _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    idx = V2Index()
    if idx.n != N_LIBRARY:
        raise SystemExit(f"extraction has {idx.n} scenes, expected {N_LIBRARY}")
    with (JW_STAGE / "selection_v2.pkl").open("rb") as handle:
        selection = pickle.load(handle)

    channels = build_channels(idx, selection)
    channels = migrate(channels)
    sanity(channels, idx)
    emit(channels, idx, timestamp)
    compile_preview()

    dispositions = Counter(c["disposition"] for c in channels)
    print(f"finalize: {len(channels)} channels -> {FINAL_CSV}")
    print(f"families: {dict(Counter(c['family'] for c in channels).most_common())}")
    print(f"sections: {dict(Counter(section_of(c['family']) for c in channels))}")
    print(f"dispositions: {dict(dispositions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
