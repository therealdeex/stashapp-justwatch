#!/usr/bin/env python3
"""Compile the owner's channel CSV into the plugin's networks.json.

The authoring CSV (``data/proposed_channels_new_taxonomy_scene_validated.csv``)
is the authoring artifact: one row per network, scene-count validated against
the library. This script is the only way networks.json changes; never hand-edit
the output.

Deterministic by design: ids and seeds derive from sha1 of the row identity,
so re-importing an unchanged CSV produces a byte-identical file and every
client keeps its rotation ordering.

Identity contract (stable_key, v1):

* blank/missing ``stable_key`` -> legacy derivation, byte-exact:
  ``id = "net_" + sha1("<number>|<name>")[:8]``,
  ``seed = int(sha1("seed|<number>|<name>")[:8], 16) % 2^31``
* present ``stable_key`` -> the key is the identity basis:
  ``id = "net_" + sha1("<stable_key>")[:8]``,
  ``seed = int(sha1("seed|<stable_key>")[:8], 16) % 2^31``

A survivor migrating onto stable_key therefore writes
``stable_key="<old number>|<old production name>"`` and keeps its id and seed
byte-for-byte; afterwards display-name and channel-number changes can never
reshuffle its rotation. stable_key is immutable once a channel reaches
production — respelling it creates a new network identity. New channels use
semantic keys over canonical ids (``jw:v1:tagpair:7978:8027``), never display
text.

Row semantics the runtime relies on (enforced here, projected in
justwatch/lineup.py):

* include tags: a scene must have ALL of them (intersection, hierarchical)
* include performers: ALL (intersection) or ANY (union) per the logic column
* include studios: ALL or ANY per the logic column; hierarchical either way.
  A single-studio row always compiles to the legacy ``studios`` shape so
  existing rows keep their source identity and rotation version.
* exclude tags: a scene must have NONE of them (any-of exclusion)
* metadata criteria: scene date range, minimum duration, created-at recency
  (``created_within_days`` stays dynamic at query time)
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO / "data" / "proposed_channels_new_taxonomy_scene_validated.csv"
OUTPUT = REPO / "justwatch" / "networks.json"

DIGITS_RE = re.compile(r"^[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Section assignment + brand identity per CSV family. Glyphs come from the
#: contract's shared FA set (they must render identically on the TV dial).
#: Families are enumerated exactly — no prefix matching: a new family is a
#: deliberate change here, never a typo sailing through.
FAMILY_SECTIONS = {
    "performer_spotlight": "performers",
    "performer_tag": "performers",
    "performer_pair": "performers",
    "studio_spotlight": "studios",
    "studio_tag": "studios",
    "tag_spotlight": "general",
    "tag_pair": "general",
    "tag_triple": "general",
    "tag_include_exclude": "general",
    "special_one_scene": "general",
    "special_rare_performers": "general",
    "special_prolific": "general",
    "special_fringe_studios": "general",
    "special_era": "general",
    "special_duration": "general",
    "special_recent": "general",
}

SECTION_BRAND = {
    "performers": {"glyph": "\uf007", "color": "#8E24AA"},   # user, purple
    "studios": {"glyph": "\uf3a5", "color": "#00897B"},      # film, teal
    "general": {"glyph": "\uf005", "color": "#EF6C00"},      # star, deep orange
}

#: Network names may run longer than the editor's catalog cap (MAX_NAME_LEN
#: 60): triple-tag rows spell out all their tags. 80 keeps guide rows sane.
MAX_NAME_LEN = 80
MIN_NUMBER = 100

MAX_INT = 2_147_483_647


def fail(message: str) -> None:
    print(f"import_channels: {message}", file=sys.stderr)
    raise SystemExit(1)


def split_ids(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split("|") if part.strip()]


def split_names(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split("|") if part.strip()]


def stable_id(number: int, name: str, stable_key: str = "") -> str:
    basis = stable_key.strip() or f"{number}|{name}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()
    return f"net_{digest[:8]}"


def stable_seed(number: int, name: str, stable_key: str = "") -> int:
    basis = stable_key.strip() or f"{number}|{name}"
    digest = hashlib.sha1(f"seed|{basis}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2_147_483_648


def _logic(row: dict, column: str) -> str:
    return (row.get(column) or "").strip().upper()


def build_source(row: dict) -> dict:
    """One composite ``filter`` source from a CSV row's id columns."""
    tags = sorted({*split_ids(row["include_tag_ids_all"])})
    exclude_tags = sorted({*split_ids(row["exclude_tag_ids_any"])})
    performer_ids = sorted({*split_ids(row["include_performer_ids_all"])})
    studio_ids = sorted({*split_ids(row["include_studio_ids_any"])})
    for label, ids in (("tags", tags), ("excludeTags", exclude_tags),
                       ("performers", performer_ids), ("studios", studio_ids)):
        if any(not DIGITS_RE.match(i) for i in ids):
            fail(f"row {row['channel_number']}: non-numeric {label} id in {ids}")
    source: dict = {"type": "filter"}
    if tags:
        source["tags"] = tags
    if exclude_tags:
        source["excludeTags"] = exclude_tags
    if performer_ids:
        # ALL -> intersection (INCLUDES_ALL); ANY -> union (INCLUDES).
        if _logic(row, "include_performer_logic") == "ANY":
            source["performersAny"] = performer_ids
        else:
            source["performers"] = performer_ids
    if studio_ids:
        # A single-studio row always keeps the legacy ``studios`` shape (one
        # subtree, INCLUDES_ALL == INCLUDES there) so existing rows keep their
        # source identity; multi-studio rows branch on the logic column.
        if len(studio_ids) > 1 and _logic(row, "include_studio_logic") == "ANY":
            source["studiosAny"] = studio_ids
        else:
            source["studios"] = studio_ids
    source.update(_metadata_source(row))
    if not any(source.get(key) for key in (
            "tags", "performers", "performersAny", "studios", "studiosAny",
            "date", "duration", "createdAt")):
        fail(f"row {row['channel_number']}: no include criteria")
    return source


def _metadata_source(row: dict) -> dict:
    """Metadata criteria (scene date / duration / created-at recency).

    All optional; each present field is validated here so a malformed authoring
    row fails at import, never at air time.
    """
    out: dict = {}
    number = row["channel_number"]
    date_from = (row.get("scene_date_from") or "").strip()
    date_to = (row.get("scene_date_to") or "").strip()
    if date_from or date_to:
        if not (date_from and date_to):
            fail(f"row {number}: scene date needs both bounds "
                 f"(got from={date_from!r}, to={date_to!r})")
        for bound in (date_from, date_to):
            if not DATE_RE.match(bound):
                fail(f"row {number}: scene date bound {bound!r} is not YYYY-MM-DD")
        try:
            _dt.date.fromisoformat(date_from)
            _dt.date.fromisoformat(date_to)
        except ValueError as exc:
            fail(f"row {number}: invalid scene date ({exc})")
        out["date"] = {"from": date_from, "to": date_to}
    duration = (row.get("duration_min_seconds") or "").strip()
    if duration:
        if not duration.isdigit():
            fail(f"row {number}: duration_min_seconds {duration!r} is not a number")
        out["duration"] = {"min": int(duration)}
    days = (row.get("created_within_days") or "").strip()
    if days:
        if not days.isdigit() or int(days) <= 0:
            fail(f"row {number}: created_within_days {days!r} must be a positive number")
        out["createdAt"] = {"withinDays": int(days)}
    return out


def check_logic(row: dict) -> None:
    """The CSV's logic flags must say what the runtime projects; anything else
    means the authoring tool changed semantics and this importer must be
    revisited, not silently obeyed."""
    number = row["channel_number"]
    if row["include_tag_logic"] not in ("", "ALL"):
        fail(f"row {number}: include_tag_logic {row['include_tag_logic']!r} (expected ALL)")
    if _logic(row, "include_performer_logic") not in ("", "ALL", "ANY"):
        fail(f"row {number}: include_performer_logic "
             f"{row['include_performer_logic']!r} (expected ALL or ANY)")
    studio_logic = _logic(row, "include_studio_logic")
    if studio_logic not in ("", "ALL", "ANY"):
        fail(f"row {number}: include_studio_logic "
             f"{row['include_studio_logic']!r} (expected ALL or ANY)")
    if len(split_ids(row["include_studio_ids_any"])) > 1 and not studio_logic:
        fail(f"row {number}: more than one include studio needs an explicit "
             f"include_studio_logic (ALL or ANY)")
    if row["exclude_tag_logic"] not in ("", "ANY"):
        fail(f"row {number}: exclude_tag_logic {row['exclude_tag_logic']!r} (expected ANY)")


def _any_summary(names: list[str], noun: str) -> str:
    if len(names) <= 4:
        return ", ".join(names) + " (any)"
    return f"{len(names):,} {noun} (any)"


def source_label(row: dict) -> str:
    parts = []
    performers = split_names(row.get("include_performers_all") or "")
    studios = split_names(row.get("include_studios_any") or "")
    tags = split_names(row.get("include_tags_all") or "")
    if performers:
        if _logic(row, "include_performer_logic") == "ANY" and len(performers) > 1:
            parts.append(_any_summary(performers, "performers"))
        else:
            parts.append(", ".join(performers))
    if studios:
        if _logic(row, "include_studio_logic") == "ANY" and len(studios) > 1:
            parts.append(_any_summary(studios, "studios"))
        else:
            parts.append(", ".join(studios))
    if tags:
        parts.append(" × ".join(tags))
    parts.extend(_metadata_label(_metadata_source(row)))
    label = " · ".join(p for p in parts if p)
    exclude_names = split_names(row.get("exclude_tags_any") or "")
    if exclude_names:
        label += f" (without {', '.join(exclude_names)})"
    return label


def _metadata_label(metadata: dict) -> list[str]:
    parts = []
    date = metadata.get("date")
    if isinstance(date, dict) and date.get("from") and date.get("to"):
        parts.append(f"{date['from'][:4]}–{date['to'][:4]}")
    duration = metadata.get("duration")
    if isinstance(duration, dict) and duration.get("min"):
        parts.append(f"{duration['min'] // 60}+ min")
    created = metadata.get("createdAt")
    if isinstance(created, dict) and created.get("withinDays"):
        parts.append(f"last {created['withinDays']} days")
    return parts


def import_csv(csv_path: Path) -> dict:
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        fail("CSV has no rows")

    channels = []
    seen_numbers: set[int] = set()
    seen_keys: set[str] = set()
    seen_ids: dict[str, int] = {}
    for row in rows:
        number = int(row["channel_number"])
        name = row["channel_name"].strip()
        family = row["channel_family"].strip()
        stable_key = (row.get("stable_key") or "").strip()
        if number < MIN_NUMBER:
            fail(f"row {number}: below the network band (custom owns 1-99)")
        if number in seen_numbers:
            fail(f"row {number}: duplicate channel number")
        seen_numbers.add(number)
        if not name:
            fail(f"row {number}: empty name")
        if len(name) > MAX_NAME_LEN:
            fail(f"row {number}: name longer than {MAX_NAME_LEN} characters: {name!r}")
        if family not in FAMILY_SECTIONS:
            fail(f"row {number}: unknown channel_family {family!r}")
        if stable_key:
            if stable_key in seen_keys:
                fail(f"row {number}: duplicate stable_key {stable_key!r}")
            seen_keys.add(stable_key)
        check_logic(row)
        section = FAMILY_SECTIONS[family]
        brand = SECTION_BRAND[section]
        channel_id = stable_id(number, name, stable_key)
        if channel_id in seen_ids:
            fail(f"row {number}: network id {channel_id} collides with row "
                 f"{seen_ids[channel_id]}")
        seen_ids[channel_id] = number
        channels.append({
            "id": channel_id,
            "number": number,
            "name": name,
            "glyph": brand["glyph"],
            "color": brand["color"],
            "section": section,
            "family": family,
            "count": int(row["exact_scene_count"]),
            "sort": "shuffle",
            "seed": stable_seed(number, name, stable_key),
            "programmingMode": "fixed",
            "sourceLabel": source_label(row),
            "source": build_source(row),
            "rationale": row["rationale"].strip(),
        })

    channels.sort(key=lambda c: c["number"])
    try:
        origin = str(csv_path.relative_to(REPO))
    except ValueError:  # an authoring CSV outside this repo
        origin = str(csv_path)
    return {"generatedFrom": origin, "channels": channels}


def revision(document: dict) -> str:
    canonical = json.dumps(document["channels"], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile the authoring channel CSV into networks.json.")
    parser.add_argument("csv", nargs="?", type=Path, default=None,
                        help="authoring CSV (default: the production CSV)")
    parser.add_argument("--csv", dest="csv_flag", type=Path, default=None,
                        help="authoring CSV (flag form; wins over the positional)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output path (default: the live justwatch/networks.json)")
    args = parser.parse_args(argv)
    csv_path = args.csv_flag or args.csv or DEFAULT_CSV
    output = args.out or OUTPUT
    if not csv_path.exists():
        fail(f"no CSV at {csv_path}")
    document = import_csv(csv_path)
    document["revision"] = revision(document)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    sections: dict[str, int] = {}
    for channel in document["channels"]:
        sections[channel["section"]] = sections.get(channel["section"], 0) + 1
    summary = ", ".join(f"{count} {name}" for name, count in sorted(sections.items()))
    print(f"import_channels: {len(document['channels'])} networks -> {output} "
          f"(revision {document['revision']}: {summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
