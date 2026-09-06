#!/usr/bin/env python3
"""Compile the owner's channel CSV into the plugin's networks.json.

The CSV (``data/proposed_channels_scene_validated.csv``) is the authoring
artifact: one row per network, scene-count validated against the library.
This script is the only way networks.json changes; never hand-edit the output.

Deterministic by design: ids and seeds derive from sha1 of the row identity,
so re-importing an unchanged CSV produces a byte-identical file and every
client keeps its rotation ordering.

Row semantics the runtime relies on (enforced here, projected in
justwatch/lineup.py):

* include tags/performers: a scene must have ALL of them (intersection)
* include studios: a scene must belong to the (single) studio
* exclude tags: a scene must have NONE of them (any-of exclusion)
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO / "data" / "proposed_channels_scene_validated.csv"
OUTPUT = REPO / "justwatch" / "networks.json"

DIGITS_RE = re.compile(r"^[0-9]+$")

#: Section assignment + brand identity per CSV family. Glyphs come from the
#: contract's shared FA set (they must render identically on the TV dial).
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


def fail(message: str) -> None:
    print(f"import_channels: {message}", file=sys.stderr)
    raise SystemExit(1)


def split_ids(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split("|") if part.strip()]


def split_names(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split("|") if part.strip()]


def stable_id(number: int, name: str) -> str:
    digest = hashlib.sha1(f"{number}|{name}".encode("utf-8")).hexdigest()
    return f"net_{digest[:8]}"


def stable_seed(number: int, name: str) -> int:
    digest = hashlib.sha1(f"seed|{number}|{name}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2_147_483_648


def build_source(row: dict) -> dict:
    """One composite ``filter`` source from a CSV row's id columns."""
    tags = sorted({*split_ids(row["include_tag_ids_all"])})
    exclude_tags = sorted({*split_ids(row["exclude_tag_ids_any"])})
    performers = sorted({*split_ids(row["include_performer_ids_all"])})
    studios = sorted({*split_ids(row["include_studio_ids_any"])})
    for label, ids in (("tags", tags), ("excludeTags", exclude_tags),
                       ("performers", performers), ("studios", studios)):
        if any(not DIGITS_RE.match(i) for i in ids):
            fail(f"row {row['channel_number']}: non-numeric {label} id in {ids}")
    if not (tags or performers or studios):
        fail(f"row {row['channel_number']}: no include criteria")
    source: dict = {"type": "filter"}
    if tags:
        source["tags"] = tags
    if exclude_tags:
        source["excludeTags"] = exclude_tags
    if performers:
        source["performers"] = performers
    if studios:
        source["studios"] = studios
    return source


def check_logic(row: dict) -> None:
    """The CSV's logic flags must say what the runtime projects; anything else
    means the authoring tool changed semantics and this importer must be
    revisited, not silently obeyed."""
    number = row["channel_number"]
    if row["include_tag_logic"] not in ("", "ALL"):
        fail(f"row {number}: include_tag_logic {row['include_tag_logic']!r} (expected ALL)")
    if row["include_performer_logic"] not in ("", "ALL"):
        fail(f"row {number}: include_performer_logic {row['include_performer_logic']!r} (expected ALL)")
    if len(split_ids(row["include_studio_ids_any"])) > 1:
        fail(f"row {number}: more than one include studio (runtime is single-studio)")
    if row["exclude_tag_logic"] not in ("", "ANY"):
        fail(f"row {number}: exclude_tag_logic {row['exclude_tag_logic']!r} (expected ANY)")


def source_label(row: dict) -> str:
    parts = []
    performers = split_names(row["include_performers_all"])
    studios = split_names(row["include_studios_any"])
    tags = split_names(row["include_tags_all"])
    if performers:
        parts.append(", ".join(performers))
    if studios:
        parts.append(", ".join(studios))
    if tags:
        parts.append(" × ".join(tags))
    label = " · ".join(parts)
    exclude_names = split_names(row["exclude_tags_any"])
    if exclude_names:
        label += f" (without {', '.join(exclude_names)})"
    return label


def import_csv(csv_path: Path) -> dict:
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        fail("CSV has no rows")

    channels = []
    seen_numbers: set[int] = set()
    for row in rows:
        number = int(row["channel_number"])
        name = row["channel_name"].strip()
        family = row["channel_family"].strip()
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
        check_logic(row)
        section = FAMILY_SECTIONS[family]
        brand = SECTION_BRAND[section]
        channels.append({
            "id": stable_id(number, name),
            "number": number,
            "name": name,
            "glyph": brand["glyph"],
            "color": brand["color"],
            "section": section,
            "family": family,
            "count": int(row["exact_scene_count"]),
            "sort": "shuffle",
            "seed": stable_seed(number, name),
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


def main() -> int:
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        fail(f"no CSV at {csv_path}")
    document = import_csv(csv_path)
    document["revision"] = revision(document)
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    sections: dict[str, int] = {}
    for channel in document["channels"]:
        sections[channel["section"]] = sections.get(channel["section"], 0) + 1
    summary = ", ".join(f"{count} {name}" for name, count in sorted(sections.items()))
    print(f"import_channels: {len(document['channels'])} networks -> {OUTPUT} "
          f"(revision {document['revision']}: {summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
