"""Server-side computation of the TV app's auto-generated channels.

A faithful port of the Just Watch tiering in the Android app
(JustWatchViewModel.buildPerformerSection / buildStudioSection / tag groups,
JustWatchDial.aliasScore) so the Channel Studio can show the SAME channels the
TV generates — Studios, Tags, and Performers — not just the custom 1–99 band.
The TV app still computes these client-side; this port exists for display and
tuning in the editor (and later, offloading the TV).

Data fetching is injected (callables) so the tiering is unit-testable without
a Stash server. The curated specs live in ``specs.json``, generated from the
app's ``JustWatchChannelSpecs.kt`` by ``tools/extract_specs.py`` — one source
of truth, never hand-edited.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

_SPECS_PATH = Path(__file__).resolve().parent / "specs.json"
_SPECS: dict | None = None

STUDIO_GROUP_SCORE_FLOOR = 3.0
STUDIO_SECOND_SLOT_RATIO = 0.2
STUDIO_SAMPLE_SCENES = 200

# Ordinary channels number base+0 .. base+97. The 99th number of each entity
# tier (399 / 499) is pinned to the spillover channel, so ordinary numbering
# must stop one short of it or the two would collide at scale.
NUMBERED_PER_SECTION = 98


def specs() -> dict:
    global _SPECS
    if _SPECS is None:
        _SPECS = json.loads(_SPECS_PATH.read_text(encoding="utf-8"))
    return _SPECS


# ---------------------------------------------------------------------------
# Alias matching (port of JustWatchDial.aliasScore / bestAliasScore / matchTags)
# ---------------------------------------------------------------------------


def normalize(name: str) -> str:
    return "".join(ch for ch in name.strip().lower() if ch.isalnum())


def alias_score(alias: str, tag_name: str) -> int:
    """Exact 3, prefix 2, substring 1 (long aliases only)."""
    a = normalize(alias)
    n = normalize(tag_name)
    if not a or not n:
        return 0
    if n == a:
        return 3
    if n.startswith(a) or a.startswith(n):
        return 2
    if len(a) >= 5 and (a in n or n in a):
        return 1
    return 0


def best_alias_score(aliases: list[str], tag_name: str) -> int:
    return max((alias_score(a, tag_name) for a in aliases), default=0)


def match_tags(
    aliases: list[str], tags: list[dict],
) -> list[dict]:
    """Tags matching any alias, best-score first, scene count breaking ties.

    ``tags`` entries carry ``name`` and (optionally) ``sceneCount``.
    """
    scored = []
    for tag in tags:
        best = best_alias_score(aliases, tag.get("name") or "")
        if best > 0:
            scored.append((best, tag.get("sceneCount") or 0, tag))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [tag for _, _, tag in scored]


def tag_weight(name: str) -> float:
    """Demographic code families are scene dressing, not a studio's brand."""
    return (
        0.5
        if name.startswith(("CAST:", "AGE:", "DEMO:", "BODY:"))
        else 1.0
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def tag_group_channels(all_tags: list[dict]) -> list[dict]:
    """The General section's tag-group band (201+). ``all_tags`` = every server
    tag with scene counts."""
    rows = []
    for spec in specs()["tagChannels"]:
        if spec["number"] < 1:
            continue
        matched = match_tags(spec["aliases"], all_tags)
        total = sum(t.get("sceneCount") or 0 for t in matched)
        rows.append({
            "number": 200 + spec["number"],
            "name": spec["name"],
            "kind": "tags",
            "count": total,
            "offAir": not matched or total == 0,
        })
    return rows


def _entity_solo_rows(
    entities: list[dict], solo_threshold: int, group_threshold: int, kind: str,
) -> list[dict]:
    rows = []
    for entity in entities:
        count = entity.get("sceneCount") or 0
        if count >= solo_threshold or 3 <= count <= group_threshold:
            rows.append({
                "kind": kind,
                "id": entity["id"],
                "name": entity["name"],
                "count": count,
            })
    return rows


def performer_channels(
    performers: list[dict], solo_threshold: int, group_threshold: int,
) -> list[dict]:
    """Performer tiers. ``performers`` = every performer (scene_count > 0) with
    ``tags`` (list of tag names), pre-sorted by scene count descending."""
    performers = [
        p for p in performers if (p.get("sceneCount") or 0) > 0
    ]
    performers.sort(key=lambda p: -(p.get("sceneCount") or 0))
    group_specs = [
        s for s in specs()["performerGroups"] if s["number"] >= 1
    ]
    # The Ensemble: the last empty-alias spec — the explicit catch-all.
    ensemble = max(
        (s["number"] for s in specs()["performerGroups"] if not s["aliases"]),
        default=0,
    )

    rows = _entity_solo_rows(performers, solo_threshold, group_threshold, "performer")

    assignments: dict[int, list[str]] = {}
    spillover: list[str] = []
    for performer in performers:
        count = performer.get("sceneCount") or 0
        if not (group_threshold < count < solo_threshold):
            continue
        performer_tags = [
            {"name": name, "sceneCount": 0} for name in performer.get("tags") or []
        ]
        matched = [
            spec["number"]
            for spec in group_specs
            if spec["aliases"] and match_tags(spec["aliases"], performer_tags)
        ]
        if not matched:
            assignments.setdefault(ensemble, []).append(performer["id"])
        else:
            for n in matched:
                assignments.setdefault(n, []).append(performer["id"])
    for performer in performers:
        if (performer.get("sceneCount") or 0) in (1, 2):
            spillover.append(performer["id"])

    for spec in group_specs:
        members = assignments.get(spec["number"]) or []
        if members:
            rows.append({
                "kind": "performerGroup",
                "id": f"group:{spec['number']}",
                "name": spec["name"],
                "members": len(members),
                "count": None,
            })
    if spillover:
        rows.append({
            "kind": "performerSpillover",
            "id": "group:0",
            "name": specs()["performerSpilloverName"],
            "members": len(spillover),
            "count": None,
        })
    return _number_rows(rows, base=401, spillover_number=499)


def studio_channels(
    studios: list[dict],
    solo_threshold: int,
    group_threshold: int,
    scene_tag_sample: Callable[[str], list[str]],
) -> list[dict]:
    """Studio tiers, mirroring performers with the tag-signal group scoring.

    ``studios`` = every studio (scene_count > 0) with ``tags`` (names), sorted
    by scene count descending. ``scene_tag_sample(studio_id)`` returns tag
    names of up to STUDIO_SAMPLE_SCENES of the studio's scenes (oldest first).
    """
    studios = [s for s in studios if (s.get("sceneCount") or 0) > 0]
    studios.sort(key=lambda s: -(s.get("sceneCount") or 0))
    group_specs = [
        s for s in specs()["studioGroups"] if s["number"] >= 1
    ]

    rows = _entity_solo_rows(studios, solo_threshold, group_threshold, "studio")

    assignments: dict[int, list[str]] = {}
    spillover: list[str] = []
    for studio in studios:
        count = studio.get("sceneCount") or 0
        if not (group_threshold < count < solo_threshold):
            continue
        qualified = _score_studio_groups(studio, scene_tag_sample(studio["id"]), group_specs)
        if not qualified:
            spillover.append(studio["id"])
        else:
            for n in qualified:
                assignments.setdefault(n, []).append(studio["id"])
    for studio in studios:
        if (studio.get("sceneCount") or 0) in (1, 2):
            spillover.append(studio["id"])

    for spec in group_specs:
        members = assignments.get(spec["number"]) or []
        if members:
            rows.append({
                "kind": "studioGroup",
                "id": f"group:{spec['number']}",
                "name": spec["name"],
                "members": len(members),
                "count": None,
            })
    if spillover:
        rows.append({
            "kind": "studioSpillover",
            "id": "group:0",
            "name": specs()["studioSpilloverName"],
            "members": len(spillover),
            "count": None,
        })
    return _number_rows(rows, base=301, spillover_number=399)


def _score_studio_groups(
    studio: dict, scene_tag_names: list[str], group_specs: list[dict],
) -> list[int]:
    """Weighted tag-signal scoring (port of scoreStudioGroups): a studio's own
    tags count once per total scene; each sampled scene tag adds 1."""
    signal: dict[str, list] = {}  # name -> [count, weight]

    def add(name: str, count: int) -> None:
        if name.startswith("CURATOR:"):
            return
        weight = (
            0.5
            if name.startswith(("CAST:", "AGE:", "DEMO:", "BODY:"))
            else 1.0
        )
        signal[name] = [count, weight]

    for name in studio.get("tags") or []:
        add(name, studio.get("sceneCount") or 0)
    for name in scene_tag_names:
        existing = signal.get(name)
        if existing is None:
            signal[name] = [1, tag_weight(name)]
        else:
            existing[0] += 1
    if not signal:
        return []

    candidates = []
    for spec in group_specs:
        if not spec["aliases"]:
            continue
        score = 0.0
        distinct = 0
        exact = 0
        for name, (count, weight) in signal.items():
            best = max(alias_score(a, name) for a in spec["aliases"])
            if best > 0:
                distinct += 1
                if best == 3:
                    exact += 1
                score += best * count * weight
        if score >= STUDIO_GROUP_SCORE_FLOOR:
            candidates.append((score, distinct, exact, spec["number"]))
    if not candidates:
        return []
    candidates.sort(key=lambda c: (-c[0], -c[1], -c[2], c[3]))
    result = [candidates[0][3]]
    if len(candidates) > 1 and candidates[1][0] >= candidates[0][0] * STUDIO_SECOND_SLOT_RATIO:
        result.append(candidates[1][3])
    return result


def _number_rows(rows: list[dict], base: int, spillover_number: int) -> list[dict]:
    """Display numbers: section ordinal (capped), spillover pinned at the end."""
    ordinal = 0
    for row in rows:
        if row["kind"].endswith("Spillover"):
            row["number"] = spillover_number
        else:
            row["number"] = base + ordinal if ordinal < NUMBERED_PER_SECTION else None
            ordinal += 1
    return rows


# ---------------------------------------------------------------------------
# Fetch wiring (GraphQL callables live in main.py; kept here for cohesion)
# ---------------------------------------------------------------------------

FULL_DIRECTORY_QUERIES = {
    "tags": """
query JustWatchAllTags { findTags(filter: {per_page: -1}) { tags { id name scene_count } } }
""",
    "performers": """
query JustWatchAllPerformers {
  findPerformers(filter: {per_page: -1}) { performers { id name scene_count tags { name } } }
}
""",
    "studios": """
query JustWatchAllStudios {
  findStudios(filter: {per_page: -1}) { studios { id name scene_count tags { name } } }
}
""",
    "studioSceneTags": """
query JustWatchStudioSample($id: ID!) {
  findScenes(
    filter: {per_page: %d, sort: "date", direction: ASC}
    scene_filter: {studios: {value: [$id], modifier: INCLUDES, depth: -1}}
  ) { scenes { tags { name } } }
}
""" % STUDIO_SAMPLE_SCENES,
}


def build_full_directory(submit: Callable[[str, dict], dict], settings: dict) -> dict:
    """Compute every auto section. ``submit(query, variables)`` is the GraphQL
    client's submit."""
    solo = int(settings.get("soloThreshold") or 10)
    group = int(settings.get("groupThreshold") or 5)

    tags_data = submit(FULL_DIRECTORY_QUERIES["tags"], {}) or {}
    all_tags = [
        {"id": t["id"], "name": t["name"], "sceneCount": t.get("scene_count") or 0}
        for t in ((tags_data.get("findTags") or {}).get("tags") or [])
    ]

    performers_data = submit(FULL_DIRECTORY_QUERIES["performers"], {}) or {}
    performers = [
        {
            "id": p["id"],
            "name": p["name"],
            "sceneCount": p.get("scene_count") or 0,
            "tags": [t.get("name") or "" for t in (p.get("tags") or [])],
        }
        for p in ((performers_data.get("findPerformers") or {}).get("performers") or [])
    ]

    studios_data = submit(FULL_DIRECTORY_QUERIES["studios"], {}) or {}
    studios = [
        {
            "id": s["id"],
            "name": s["name"],
            "sceneCount": s.get("scene_count") or 0,
            "tags": [t.get("name") or "" for t in (s.get("tags") or [])],
        }
        for s in ((studios_data.get("findStudios") or {}).get("studios") or [])
    ]
    mid_ids = {
        s["id"]
        for s in studios
        if group < (s.get("sceneCount") or 0) < solo
    }

    def scene_tag_sample(studio_id: str) -> list[str]:
        if studio_id not in mid_ids:
            return []
        data = submit(
            FULL_DIRECTORY_QUERIES["studioSceneTags"], {"id": studio_id},
        ) or {}
        names: list[str] = []
        for scene in ((data.get("findScenes") or {}).get("scenes") or []):
            for tag in scene.get("tags") or []:
                names.append(tag.get("name") or "")
        return names

    return {
        "general": {
            "tagChannels": tag_group_channels(all_tags),
        },
        "studios": {
            "channels": studio_channels(studios, solo, group, scene_tag_sample),
        },
        "performers": {
            "channels": performer_channels(performers, solo, group),
        },
    }
