"""Lineup engine: turn a channel's source + sort into scene pages.

Custom channels air exactly what the owner authored: the source criterion is
used verbatim and global section include/exclude tags are deliberately NOT
applied (those tune the TV app's auto-generated channels, not user channels).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from justwatch import contract

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
    source comparison must go through this so those still match.
    """
    if source.get("type") == "tag":
        return ("tag", tuple(tag_ids(source)))
    return (str(source.get("type")), str(source.get("id", "")))


def build_scene_filter(source: dict, object_filter: dict | None = None) -> dict:
    """Project a channel source into a ``SceneFilterType`` variable.

    ``object_filter`` carries a saved filter's stored ``object_filter`` (used
    verbatim); entity sources project directly.
    """
    source_type = source.get("type")
    source_id = str(source.get("id", ""))
    if source_type == "savedFilter":
        if not isinstance(object_filter, dict):
            raise LookupError("saved filter has no stored object filter")
        return object_filter
    if source_type == "tag":
        # INCLUDES with several values is a union: a scene tagged with ANY of
        # them airs (depth -1 keeps sub-tags of every pill in the set).
        return {"tags": {"value": tag_ids(source), "modifier": "INCLUDES", "depth": -1}}
    if source_type == "performer":
        return {"performers": {"value": [source_id], "modifier": "INCLUDES"}}
    if source_type == "studio":
        return {"studios": {"value": [source_id], "modifier": "INCLUDES", "depth": -1}}
    raise ValueError(f"unknown source type: {source_type!r}")


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


def rotation_version(source: dict, sort: str, seed: int, size: int) -> str:
    """Stable identity of a rotation's ordering: same inputs, same loop.

    Callers suffix the catalog revision so a save republishes every rotation;
    clients compare versions to drop stale cached lineups.
    """
    basis = json.dumps(
        [*source_key(source), sort, int(seed), int(size)],
        sort_keys=True,
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


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
