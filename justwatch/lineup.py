"""Lineup engine: turn a channel's source + sort into scene pages.

Custom channels air exactly what the owner authored: the source criterion is
used verbatim and global section include/exclude tags are deliberately NOT
applied (those tune the TV app's auto-generated channels, not user channels).
"""

from __future__ import annotations

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
        return {"tags": {"value": [source_id], "modifier": "INCLUDES", "depth": -1}}
    if source_type == "performer":
        return {"performers": {"value": [source_id], "modifier": "INCLUDES"}}
    if source_type == "studio":
        return {"studios": {"value": [source_id], "modifier": "INCLUDES", "depth": -1}}
    raise ValueError(f"unknown source type: {source_type!r}")


def build_find_filter(sort: str, seed: int, page: int, per_page: int) -> dict:
    """Project a channel sort into a ``FindFilterType`` variable."""
    spec = contract.SORTS.get(sort)
    if spec is None:
        raise ValueError(f"unknown sort: {sort!r}")
    out: dict[str, Any] = {"page": page, "per_page": per_page}
    if sort == "shuffle":
        out["sort"] = f"random_{seed}"
        out["direction"] = "DESC"
    else:
        out["sort"] = spec["sort"]
        out["direction"] = spec["direction"]
    return out


def fetch_lineup(
    client: Any,
    *,
    source: dict,
    sort: str,
    seed: int,
    page: int = 1,
    per_page: int = 50,
    include_paths: bool = False,
    object_filter: dict | None = None,
) -> dict:
    """Fetch one lineup page. Returns ``{total, items}`` where items carry
    ``id``/``title``/``duration`` (and ``preview`` when ``include_paths``).

    Scenes without a usable file duration are dropped: they cannot be
    scheduled (the TV broadcast schedule paces on duration) and cannot play.
    ``total`` remains the server's unfiltered count and may exceed the sum of
    playable items; the TV app walks items sequentially, so this is safe.
    """
    scene_filter = build_scene_filter(source, object_filter)
    variables = {
        "filter": build_find_filter(sort, seed, page, per_page),
        "scene_filter": scene_filter,
        "include_paths": bool(include_paths),
    }
    data = client.submit(FIND_SCENES, variables)
    node = (data or {}).get("findScenes") or {}
    items = []
    for scene in node.get("scenes") or []:
        files = scene.get("files") or []
        duration = max(
            (f.get("duration") or 0.0) for f in files
        ) if files else 0.0
        if not duration or duration <= 0:
            continue
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
        items.append(item)
    return {
        "total": int(node.get("count") or 0),
        "items": items,
    }


def resolve_source(client: Any, source: dict) -> tuple[str, bool]:
    """Resolve a source's display label and existence.

    Returns ``(label, exists)``. Saved filters must be SCENES-mode; a
    non-scenes filter is reported as missing (it cannot air scenes).
    """
    source_type = source.get("type")
    source_id = str(source.get("id", ""))
    if not source_id.isdigit():
        return "", False
    data = client.submit(FIND_ENTITY_NAME, {"id": source_id})
    if source_type == "tag":
        node = (data or {}).get("findTag") or {}
    elif source_type == "performer":
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


def fetch_saved_object_filter(client: Any, saved_filter_id: str) -> dict | None:
    """Load a saved filter's ``object_filter`` for lineup building."""
    if not saved_filter_id.isdigit():
        return None
    data = client.submit(FIND_SAVED_FILTER, {"id": saved_filter_id})
    node = (data or {}).get("findSavedFilter") or {}
    if str(node.get("mode") or "") != _SAVED_FILTER_MODE_SCENES:
        return None
    object_filter = node.get("object_filter")
    return object_filter if isinstance(object_filter, dict) else None
