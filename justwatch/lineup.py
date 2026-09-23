"""Lineup engine: turn a channel's source + sort into scene pages.

Custom channels air exactly what the owner authored: the source criterion is
used verbatim and global section include/exclude tags are deliberately NOT
applied (those tune the TV app's auto-generated channels, not user channels).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from justwatch import contract, criteria

#: Stash's IntCriterionInput has no GREATER_THAN_EQUALS modifier, so an
#: inclusive "min" duration is expressed as a BETWEEN with a max-int upper
#: bound. Canonical home is ``criteria`` (the single projector).
INT_MAX = criteria.INT_MAX

FIND_SCENES = """
query JustWatchLineup($filter: FindFilterType!, $scene_filter: SceneFilterType!, $include_paths: Boolean!) {
  findScenes(filter: $filter, scene_filter: $scene_filter) {
    count
    scenes {
      id
      title
      date
      studio { name }
      files { duration }
      paths @include(if: $include_paths) { preview }
    }
  }
}
"""

FIND_SAVED_FILTER = """
query JustWatchSavedFilter($id: ID!) {
  findSavedFilter(id: $id) { id name mode find_filter { q } object_filter }
}
"""

FIND_ENTITY_NAME = """
query JustWatchEntityName($id: ID!) {
  findTag(id: $id) { name }
  findPerformer(id: $id) { name }
  findStudio(id: $id) { name }
  findSavedFilter(id: $id) { name mode }
}
"""

_SAVED_FILTER_MODE_SCENES = "SCENES"

_URL_ORIGIN_RE = None


def _strip_origin(url: str) -> str:
    """Reduce an absolute Stash URL to its path+query.

    Stash builds paths.* URLs from whatever host the request used; the plugin
    reaches Stash over localhost, so absolute URLs would only resolve on the
    server's own machine. Path-only URLs let the editor resolve against the
    browser origin it was actually loaded from.
    """
    if isinstance(url, str) and "://" in url:
        parts = url.split("/", 3)
        return "/" + parts[3] if len(parts) > 3 else "/"
    return url or ""


def tag_ids(source: dict) -> list[str]:
    """A tag source's tag ids: the canonical ``ids`` set, else the single ``id``.
    Sorted numerically so equal sets are identical regardless of input order."""
    ids = source.get("ids")
    if isinstance(ids, list):
        out = {str(x) for x in ids if str(x).isdigit()}
        if out:
            return sorted(out, key=int)
    only = str(source.get("id", ""))
    return [only] if only.isdigit() else []


def source_key(source: dict) -> tuple:
    """Equality identity of a source, tolerant of legacy single-id tag shapes.

    Publications stored before multi-tag support embed ``{type, id}``; every
    source comparison must go through this so those still match. A
    ``criteria`` source keys on its full canonical rule set (two different
    criteria sources are NEVER the same key — the historical
    ``('criteria', '')`` collision collapsed distinct pools onto one cache
    entry); a ``filter`` source keeps its historical tuple, extended with
    the newer criteria fields only when present so legacy publications'
    identities never churn.
    """
    kind = source.get("type")
    if kind == "tag":
        return ("tag", tuple(tag_ids(source)))
    if kind == "criteria":
        return ("criteria", criteria.source_signature(source))
    if kind == "filter":
        return ("filter", _canonical_filter(source))
    return (str(kind), str(source.get("id", "")))


def _filter_ids(value: Any) -> list[str]:
    """A filter source's id list for one criterion: digits only, sorted, deduped."""
    if not isinstance(value, list):
        return []
    out = {str(x) for x in value if str(x).isdigit()}
    return sorted(out, key=int)


def _canonical_filter(source: dict) -> tuple:
    """Hash-stable form of a ``filter`` source: sorted id lists per criterion
    plus the metadata criteria. New criteria appear only when non-empty so a
    legacy source canonicalizes to its exact historical form (rotation
    versions never churn from an importer upgrade). ``createdAt`` contributes
    its authored ``withinDays`` (the resolved cutoff varies by day by design;
    the epoch in ``rotation_version`` covers that)."""
    parts = [
        (criterion, tuple(_filter_ids(source.get(criterion))))
        for criterion in ("tags", "excludeTags", "performers", "studios")
    ]
    for criterion in ("performersAny", "studiosAny",
                      "excludePerformers", "excludeStudios"):
        ids = _filter_ids(source.get(criterion))
        if ids:
            parts.append((criterion, tuple(ids)))
    date = source.get("date")
    if isinstance(date, dict):
        parts.append(("date", (str(date.get("from", "")), str(date.get("to", "")))))
    duration = source.get("duration")
    if isinstance(duration, dict):
        # (min,) keeps the exact historical tuple for legacy min-only rows;
        # max joins only when authored so their identity is honest.
        if duration.get("max"):
            parts.append(("duration", (int(duration.get("min", 0)),
                                       int(duration.get("max")))))
        else:
            parts.append(("duration", (int(duration.get("min", 0)),)))
    created = source.get("createdAt")
    if isinstance(created, dict):
        parts.append(("createdAt", (int(created.get("withinDays", 0)),)))
    for facet in ("studioSceneCount", "performerSceneCount"):
        spec = source.get(facet)
        if isinstance(spec, dict) and (spec.get("min") or spec.get("max")):
            parts.append((facet, (int(spec.get("min") or 0), int(spec.get("max") or 0))))
    q = source.get("q")
    if isinstance(q, str) and q.strip():
        parts.append(("q", q.strip()))
    return tuple(parts)


def created_cutoff(within_days: int, today: _dt.date | None = None) -> str:
    """The inclusive cutoff date for a created-at recency criterion
    (canonical implementation: ``criteria.created_cutoff``)."""
    return criteria.created_cutoff(within_days, today)


def effective_epoch(source: dict, today: _dt.date | None = None) -> str:
    """The day-granular epoch of a dynamic source, "" for static sources.

    Only created-at recency is dynamic today: its effective membership moves
    with the resolved cutoff even though the authored source (``withinDays``)
    never changes, so clients need the epoch to invalidate caches.
    """
    created = source.get("createdAt")
    if isinstance(created, dict):
        days = created.get("withinDays")
        if isinstance(days, int) and not isinstance(days, bool) and days >= 1:
            return created_cutoff(days, today)
    # Scene-count predicates (studios/performers by activity) also move with
    # the library; a day-granular epoch keeps client caches honest.
    for facet in ("studioSceneCount", "performerSceneCount"):
        spec = source.get(facet)
        if isinstance(spec, dict) and (spec.get("min") or spec.get("max")):
            today = today or _dt.datetime.now(_dt.timezone.utc).date()
            return today.isoformat()
    return ""


def build_scene_filter(source: dict, object_filter: dict | None = None) -> dict:
    """Project a channel source into a ``SceneFilterType`` variable.

    ONE canonical implementation exists (``criteria.build_scene_filter``) and
    EVERY consumer uses it — preview, Lineup, health, and both schedulers'
    indexers. This wrapper preserves the legacy import path (byte-stable
    public signature) while delegating, so the editor's pool preview and
    actual playback can never diverge again (audit C2): a ``criteria``
    source, an edited ``filter`` source's exclusions/duration-max/dynamic
    rows, and authored text ``q`` all reach playback identically.
    """
    return criteria.build_scene_filter(source, object_filter)


def build_find_filter(
    sort: str, seed: int, page: int, per_page: int, text_query: str | None = None,
) -> dict:
    """Project a channel sort (and optional saved-search text) into a
    ``FindFilterType`` variable.

    ``text_query`` carries a saved filter's stored text search: it is part of
    the channel's membership, so it rides along on every page fetch for both
    Lineup and health snapshots.
    """
    spec = contract.SORTS.get(sort)
    if spec is None:
        raise ValueError(f"unknown sort: {sort!r}")
    out: dict[str, Any] = {"page": page, "per_page": per_page}
    if isinstance(text_query, str) and text_query.strip():
        out["q"] = text_query.strip()
    if sort == "shuffle":
        out["sort"] = f"random_{seed}"
        out["direction"] = "DESC"
    else:
        out["sort"] = spec["sort"]
        out["direction"] = spec["direction"]
    return out


def rotation_version(
    source: dict, sort: str, seed: int, size: int, today: _dt.date | None = None,
) -> str:
    """Stable identity of a rotation's ordering: same inputs, same loop.

    Callers suffix the catalog revision so a save republishes every rotation;
    clients compare versions to drop stale cached lineups. Dynamic sources
    (created-at recency) append their resolved cutoff epoch so the version
    moves with the day; static sources keep the exact historical basis, so an
    unchanged catalog re-imports to identical versions. ``today`` is
    injectable for tests.
    """
    basis = [
        *source_key(source), sort, int(seed), int(size),
    ]
    epoch = effective_epoch(source, today)
    if epoch:
        basis.append(f"epoch:{epoch}")
    return hashlib.sha1(
        json.dumps(basis, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def _scene_item(scene: dict, include_paths: bool) -> dict | None:
    """One playable lineup entry, or None when the scene cannot play."""
    files = scene.get("files") or []
    duration = max(
        (f.get("duration") or 0.0) for f in files
    ) if files else 0.0
    if not duration or duration <= 0:
        return None
    item: dict[str, Any] = {
        "id": str(scene.get("id")),
        "title": scene.get("title") or "",
        "duration": round(float(duration), 3),
        "studio": ((scene.get("studio") or {}).get("name") or ""),
        "date": scene.get("date") or "",
    }
    if include_paths:
        paths = scene.get("paths") or {}
        item["preview"] = _strip_origin(paths.get("preview") or "")
    return item


def fetch_rotation(
    client: Any,
    *,
    source: dict,
    sort: str,
    seed: int,
    size: int | None = None,
    scan_limit: int | None = None,
    include_paths: bool = False,
    object_filter: dict | None = None,
    text_query: str | None = None,
) -> dict:
    """Assemble the channel's active rotation (contract.ROTATION_*).

    The rotation is the bounded program loop every client shares: the channel's
    own order with unplayable scenes skipped, up to ``size`` playable entries,
    scanning at most ``scan_limit`` raw rows. Paging continues past a thin page
    so unplayable scenes early in the order cannot hollow out a populated
    channel. Returns ``{items, sourceTotal, rotationComplete, rotationVersion,
    loopSeconds}``:

    * ``items`` -- playable entries in airing order (id/title/duration/studio/
      date, plus ``preview`` when ``include_paths``).
    * ``sourceTotal`` -- the server's raw count for the source (the library
      behind the rotation; unplayable scenes included).
    * ``rotationComplete`` -- False when the scan hit ``size`` or ``scan_limit``
      with source left over (the loop is then a sampled subset).
    * ``loopSeconds`` -- the rotation's total runtime; the loop length every
      client should quote.
    """
    size = contract.ROTATION_SIZE if size is None else size
    scan_limit = contract.ROTATION_SCAN_LIMIT if scan_limit is None else scan_limit
    size = max(1, int(size))
    scan_limit = max(size, int(scan_limit))
    scene_filter = build_scene_filter(source, object_filter)
    # An authored text query (filter/criteria ``q``) is membership: it rides
    # every page exactly like a saved filter's stored q.
    if text_query is None:
        text_query = criteria.text_query_of(source)
    items: list[dict] = []
    seen = 0
    source_total = 0
    exhausted = False
    page = 1
    per_page = min(100, max(size, 1))
    while len(items) < size and seen < scan_limit and not exhausted:
        variables = {
            "filter": build_find_filter(sort, seed, page, per_page, text_query),
            "scene_filter": scene_filter,
            "include_paths": bool(include_paths),
        }
        data = client.submit(FIND_SCENES, variables)
        node = (data or {}).get("findScenes") or {}
        source_total = int(node.get("count") or 0)
        rows = node.get("scenes") or []
        if not rows:
            exhausted = True
            break
        seen += len(rows)
        for scene in rows:
            item = _scene_item(scene, include_paths)
            if item is not None:
                items.append(item)
                if len(items) >= size:
                    break
        if seen >= source_total:
            exhausted = True
        page += 1
    return {
        "items": items,
        "sourceTotal": source_total,
        "rotationComplete": exhausted,
        "rotationVersion": rotation_version(source, sort, seed, size),
        "loopSeconds": round(sum(i["duration"] for i in items), 1),
    }


def fetch_saved_filter(client: Any, saved_filter_id: str) -> tuple[dict | None, str | None]:
    """A saved filter's stored membership criteria: ``(object_filter, q)``.

    Either half may be None (a filter can be text-only or object-only), but a
    missing or non-scene filter returns ``(None, None)``. Both halves must be
    applied for the channel's lineup to match what the owner saved.
    """
    if not saved_filter_id.isdigit():
        return None, None
    data = client.submit(FIND_SAVED_FILTER, {"id": saved_filter_id})
    node = (data or {}).get("findSavedFilter") or {}
    if str(node.get("mode") or "") != _SAVED_FILTER_MODE_SCENES:
        return None, None
    object_filter = node.get("object_filter")
    find_filter = node.get("find_filter")
    q = find_filter.get("q") if isinstance(find_filter, dict) else None
    if not isinstance(q, str) or not q.strip():
        q = None
    return (
        object_filter if isinstance(object_filter, dict) else None,
        q,
    )


def resolve_saved_criteria(client: Any, saved_filter_id: str) -> tuple[dict, str | None]:
    """Resolved criteria for a ``savedFilter`` channel source.

    Returns ``(object_filter, q)`` with an empty (but present) object filter for
    text-only saved searches, so the caller can always build a lineup. Raises
    ``LookupError`` when the filter is missing or not a scene filter.
    """
    object_filter, q = fetch_saved_filter(client, saved_filter_id)
    if object_filter is None and not q:
        raise LookupError("saved filter not found (or not a scene filter)")
    return (object_filter if object_filter is not None else {}), q


def resolve_source(client: Any, source: dict) -> tuple[str, bool]:
    """Resolve a source's display label and existence.

    Returns ``(label, exists)``. Saved filters must be SCENES-mode; a
    non-scenes filter is reported as missing (it cannot air scenes). A tag set
    exists when ANY of its tags exists (the channel still airs), and its label
    names the first two tags with an overflow count.
    """
    source_type = source.get("type")
    source_id = str(source.get("id", ""))
    if source_type == "tag":
        names = []
        for tid in tag_ids(source):
            data = client.submit(FIND_ENTITY_NAME, {"id": tid})
            node = (data or {}).get("findTag") or {}
            name = str(node.get("name") or "").strip()
            if name:
                names.append(name)
        return _join_labels(names), bool(names)
    if not source_id.isdigit():
        return "", False
    data = client.submit(FIND_ENTITY_NAME, {"id": source_id})
    if source_type == "performer":
        node = (data or {}).get("findPerformer") or {}
    elif source_type == "studio":
        node = (data or {}).get("findStudio") or {}
    elif source_type == "savedFilter":
        node = (data or {}).get("findSavedFilter") or {}
        if str(node.get("mode") or "") != _SAVED_FILTER_MODE_SCENES:
            return "", False
    else:
        return "", False
    label = str(node.get("name") or "").strip()
    return label, bool(label)


def _join_labels(names: list[str]) -> str:
    if len(names) <= 2:
        return ", ".join(names)
    return ", ".join(names[:2]) + f" +{len(names) - 2}"
