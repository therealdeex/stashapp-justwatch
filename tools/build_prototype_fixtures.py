#!/usr/bin/env python3
"""Generate the shared prototype fixture module from the v4-final proposal.

Reads ``analysis/just-watch-final/networks.preview.json`` (the compiled 513)
plus ``data/proposed_channels_final.csv`` (for display names of entities in
the large ANY sets) and writes
``prototypes/channel-curation/shared/fixture-data.js`` as a self-contained
ES module. Run after the proposal artifacts change; never edit the generated
file by hand.

Everything the prototypes render is clearly synthetic at runtime (counts are
labeled simulated); this script only copies authored definitions and names —
no scene data, no media URLs.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREVIEW = ROOT / "analysis/just-watch-final/networks.preview.json"
AUTHORING = ROOT / "data/proposed_channels_final.csv"
OUT = ROOT / "prototypes/channel-curation/shared/fixture-data.js"

DIGITS = re.compile(r"^\d+$")


def entity_names_from_csv() -> tuple[dict, dict, dict]:
    """id -> display name maps for performers/studios/tags, from the authoring
    CSV's paired ``*_ids_*`` / ``*_<logic>`` name columns."""
    performers: dict[str, str] = {}
    studios: dict[str, str] = {}
    tags: dict[str, str] = {}

    def soak(ids_col: str, names_col: str, into: dict) -> None:
        ids = str(row.get(ids_col) or "").split("|")
        names = str(row.get(names_col) or "").split("|")
        for raw_id, raw_name in zip(ids, names):
            if not DIGITS.match(raw_id.strip()):
                continue
            into.setdefault(raw_id.strip(), raw_name.strip())

    with AUTHORING.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            soak("include_performer_ids_all", "include_performers_all", performers)
            soak("exclude_performer_ids_any", "exclude_performers_any", performers)
            soak("include_studio_ids_any", "include_studios_any", studios)
            soak("exclude_studio_ids_any", "exclude_studios_any", studios)
            soak("include_tag_ids_all", "include_tags_all", tags)
            soak("exclude_tag_ids_any", "exclude_tags_any", tags)
    return performers, studios, tags


def main() -> None:
    preview = json.loads(PREVIEW.read_text(encoding="utf-8"))
    channels = preview["channels"]
    performers, studios, tags = entity_names_from_csv()

    # The compiled rows are the seed verbatim (identity + source + seed);
    # presentation-only fields the prototypes derive are added client-side.
    slim = []
    for ch in channels:
        slim.append({
            "id": ch["id"],
            "number": ch["number"],
            "name": ch["name"],
            "glyph": ch["glyph"],
            "color": ch["color"],
            "section": ch["section"],
            "family": ch["family"],
            "seedCount": ch.get("count", 0),
            "sort": ch.get("sort", "shuffle"),
            "seed": ch["seed"],
            "programmingMode": ch.get("programmingMode", "fixed"),
            "sourceLabel": ch.get("sourceLabel", ""),
            "source": ch["source"],
        })
    slim.sort(key=lambda c: c["number"])

    synthetic = [
        {
            "id": "ch_a1b2c3d4", "number": 1, "name": "Slow Burn Mornings",
            "color": "#E91E63", "kind": "ch", "groupId": "grp_my",
            "sort": "shuffle", "seed": 402118766, "enabled": True, "archived": False,
            "source": {"type": "tag", "id": "1189", "ids": ["1189", "872"]},
            "sourceLabel": "Slow burn + Rain on Glass", "programming": {"mode": "fixed", "spacingMinutes": 240},
            "seedCount": 214, "note": "synthetic custom channel",
        },
        {
            "id": "ch_b5c6d7e8", "number": 2, "name": "Featured Studio of the Month",
            "color": "#3949AB", "kind": "ch", "groupId": "grp_my",
            "sort": "newest", "seed": 77120034, "enabled": True, "archived": False,
            "source": {"type": "savedFilter", "id": "7"},
            "sourceLabel": "Saved search: Curated picks", "programming": {"mode": "explore"},
            "seedCount": 96, "note": "synthetic custom channel (saved search source)",
        },
        {
            "id": "ch_c9d0e1f2", "number": 3, "name": "After Hours — the long cut",
            "color": "#00897B", "kind": "ch", "groupId": "grp_my",
            "sort": "longest", "seed": 1918346250, "enabled": False, "archived": False,
            "source": {"type": "performer", "id": "883"},
            "sourceLabel": "Paused for the season", "programming": {"mode": "fixed"},
            "seedCount": 58, "paused": True, "note": "synthetic paused channel",
        },
        {
            "id": "ch_d3e4f5a6", "number": 4, "name": "Archive: Late Night Marathons",
            "color": "#455A64", "kind": "ch", "groupId": "grp_my",
            "sort": "shuffle", "seed": 604417992, "enabled": True, "archived": True,
            "source": {"type": "tag", "id": "12", "ids": ["12"]},
            "sourceLabel": "Archived 2026-08", "programming": {"mode": "fixed"},
            "seedCount": 77, "note": "synthetic archived channel",
        },
        {
            "id": "ch_e7f8a9b0", "number": 7, "name": "Empty Pool Experiment",
            "color": "#F57C00", "kind": "ch", "groupId": "grp_general",
            "sort": "shuffle", "seed": 245988121, "enabled": True, "archived": False,
            "source": {"type": "tag", "id": "999999", "ids": ["999999"]},
            "sourceLabel": "tag no longer matches anything", "programming": {"mode": "fixed"},
            "seedCount": 0, "note": "fixture: honest empty pool",
        },
        {
            "id": "ch_f0a1b2c3", "number": 8, "name": "Deleted Entity Detector",
            "color": "#D32F2F", "kind": "ch", "groupId": "grp_general",
            "sort": "shuffle", "seed": 8675309, "enabled": True, "archived": False,
            "source": {"type": "performer", "id": "424242"},
            "sourceLabel": "performer deleted in Stash", "programming": {"mode": "fixed"},
            "seedCount": 0, "note": "fixture: deleted entity reference",
        },
    ]

    groups = [
        {"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
        {"id": "grp_general", "name": "General", "position": 2, "legacySection": "general"},
        {"id": "grp_studios", "name": "Studios", "position": 3, "legacySection": "studios"},
        {"id": "grp_performers", "name": "Performers", "position": 4, "legacySection": "performers"},
    ]

    out = {
        "meta": {
            "seedCatalog": "just-watch-v4-final",
            "seedRevision": preview.get("revision", ""),
            "channelCount": len(slim),
            "simulatedNotice": "Prototype data: channel definitions come from the "
                               "v4-final proposal; counts, previews and schedules are SIMULATED. "
                               "Entity display names not present in the authoring CSV are synthetic. "
                               "No production media is fetched.",
        },
        "groups": groups,
        "channels": slim,
        "customChannels": synthetic,
        "entities": {"performers": performers, "studios": studios, "tags": tags},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(
        "// Generated by tools/build_prototype_fixtures.py — do not edit.\n"
        f"export const FIXTURES = {payload};\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT} ({len(slim)} networks + {len(synthetic)} custom fixtures, "
          f"{len(performers)} performer / {len(studios)} studio / {len(tags)} tag names)")


if __name__ == "__main__":
    main()
