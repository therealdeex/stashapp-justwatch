"""The canonical content-rule model for the editable channel library.

A library channel's ``source`` is one of:

* the legacy custom shapes (``savedFilter`` / ``tag`` / ``performer`` /
  ``studio``) — stored and projected exactly as ``catalog``/``lineup`` always
  have, byte-for-byte through migration;
* the network ``filter`` composite — the compiled tier's shape
  (``tags`` ALL-of, ``performers``/``studios`` ALL-of, ``performersAny``/
  ``studiosAny`` ANY-of, ``excludeTags`` riding the tags criterion's
  ``excludes``, scene-date range, minimum duration, created-at recency);
* the new ``criteria`` shape — the visual rule builder's output. It is the
  filter shape generalised: each facet (tags / performers / studios) carries
  an ANY/ALL choice with its own storage field plus explicit exclusions, and
  multiple authored rows over one facet must NOT silently overwrite each
  other (that combination is rejected at validation, because Stash's
  ``SceneFilterType`` has one criterion per facet and cannot express
  OR-of-AND inside it).

Everything here is pure: validation and projection share this module so the
editor, the preview, and the scheduler all describe the same membership.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Any

from justwatch import contract
from justwatch.lineup import INT_MAX, created_cutoff

DIGITS_RE = re.compile(r"^[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: id-list facets of the ``criteria``/``filter`` source and their storage keys.
_ID_FACETS = ("tags", "tagsAny", "excludeTags", "performers", "performersAny",
              "excludePerformers", "studios", "studiosAny", "excludeStudios")

#: Facet pairs that share one Stash criterion: both populated is unwritable.
_EXCLUSIVE_FACETS = (("tags", "tagsAny"),
                     ("performers", "performersAny"),
                     ("studios", "studiosAny"))

SOURCE_TYPES = ("savedFilter", "tag", "performer", "studio", "filter", "criteria")


def ids(value: Any) -> list[str]:
    """Digits-only string id list, deduped and sorted numerically."""
    if not isinstance(value, list):
        return []
    out = {str(x) for x in value if not isinstance(x, bool) and DIGITS_RE.match(str(x))}
    return sorted(out, key=int)


def normalize(source: dict) -> dict:
    """Canonical stored form of ANY source shape (tolerant input, stable output).

    A legacy ``tag`` source keeps its historical ``{type, id, ids}`` form (its
    membership rides ``ids`` with ``id`` mirroring the first); a ``filter`` or
    ``criteria`` source canonicalizes every facet to a sorted id list and keeps
    only the metadata criteria actually present, so hashes and equality stay
    stable regardless of input order. Unknown source types pass through with
    just their type — validation reports them.
    """
    if not isinstance(source, dict):
        return {"type": ""}
    kind = source.get("type")
    if kind == "tag":
        pool = ids(source.get("ids") if isinstance(source.get("ids"), list) else [])
        single = source.get("id")
        if isinstance(single, (str, int)) and not isinstance(single, bool) \
                and DIGITS_RE.match(str(single)):
            pool = sorted({*pool, str(single)}, key=int)
        if not pool:
            return {"type": "tag", "id": ""}
        return {"type": "tag", "id": pool[0], "ids": pool}
    if kind in ("performer", "studio", "savedFilter"):
        single = source.get("id")
        return {"type": kind, "id": str(single) if single is not None else ""}
    if kind in ("filter", "criteria"):
        out: dict[str, Any] = {"type": "criteria" if kind == "criteria" else "filter"}
        for facet in _ID_FACETS:
            listing = ids(source.get(facet))
            if listing:
                out[facet] = listing
        date = source.get("date")
        if isinstance(date, dict) and (date.get("from") or date.get("to")):
            out["date"] = {"from": str(date.get("from") or ""),
                           "to": str(date.get("to") or "")}
        duration = source.get("duration")
        if isinstance(duration, dict):
            lo = duration.get("min")
            hi = duration.get("max")
            mins: dict[str, int] = {}
            if isinstance(lo, int) and not isinstance(lo, bool) and lo > 0:
                mins["min"] = lo
            if isinstance(hi, int) and not isinstance(hi, bool) and hi > 0:
                mins["max"] = hi
            if mins:
                out["duration"] = mins
        created = source.get("createdAt")
        if isinstance(created, dict) and isinstance(created.get("withinDays"), int) \
                and not isinstance(created.get("withinDays"), bool) \
                and created["withinDays"] > 0:
            out["createdAt"] = {"withinDays": created["withinDays"]}
        q = source.get("q")
        if isinstance(q, str) and q.strip():
            out["q"] = q.strip()
        return out
    return {"type": str(kind or "")}


def validate(source: Any) -> list[dict]:
    """Structural validation of a source. Returns ``[{path, code, message}]``.

    Semantic checks that need Stash (entity existence) are the caller's job;
    this only enforces what the stored shape itself must satisfy.
    """
    errors: list[dict] = []

    def err(path: str, code: str, message: str) -> None:
        errors.append({"path": path, "code": code, "message": message})

    if not isinstance(source, dict):
        err("source", "not_an_object", "source must be a JSON object")
        return errors
    kind = source.get("type")
    if kind not in SOURCE_TYPES:
        err("source.type", "unknown_source_type",
            f"source.type must be one of: {', '.join(SOURCE_TYPES)}")
        return errors
    if kind in ("performer", "studio", "savedFilter"):
        single = source.get("id")
        if not isinstance(single, (str, int)) or isinstance(single, bool) \
                or not DIGITS_RE.match(str(single)):
            err("source.id", "bad_source_id", "source.id must be a numeric id")
        return errors
    if kind == "tag":
        pool = ids(source.get("ids") if isinstance(source.get("ids"), list) else [])
        single = source.get("id")
        if isinstance(single, (str, int)) and not isinstance(single, bool) \
                and DIGITS_RE.match(str(single)):
            pool = [*pool, str(single)]
        if not pool:
            err("source.ids", "bad_source_ids",
                "a tag source needs at least one numeric tag id")
        return errors
    # filter / criteria
    unknown = sorted(set(source) - {"type", *_ID_FACETS, "date", "duration", "createdAt", "q"})
    if unknown:
        err("source", "unknown_fields",
            f"unknown source fields: {', '.join(unknown)}")
    for facet in _ID_FACETS:
        if facet in source and not ids(source.get(facet)):
            err(f"source.{facet}", "bad_source_ids",
                f"{facet} must be a list of numeric ids")
    for a, b in _EXCLUSIVE_FACETS:
        if ids(source.get(a)) and ids(source.get(b)):
            err("source", "conflicting_rows",
                f"one facet cannot be both ANY and ALL: {a} and {b} are both set; "
                "Stash cannot express (ANY row) AND (ALL row) for the same facet")
    # An exclusion rides its facet's include criterion; an orphan exclusion has
    # nowhere to project and would silently do nothing.
    for exclude_facet, include_facets in (
        ("excludeTags", ("tags", "tagsAny")),
        ("excludePerformers", ("performers", "performersAny")),
        ("excludeStudios", ("studios", "studiosAny")),
    ):
        if ids(source.get(exclude_facet)) and not any(ids(source.get(f)) for f in include_facets):
            err("source", "orphan_exclusion",
                f"{exclude_facet} needs an include rule on the same facet to apply to")
    date = source.get("date")
    if date is not None:
        if not isinstance(date, dict) or not (date.get("from") or date.get("to")):
            err("source.date", "bad_date", "date needs {from, to} (either may be empty)")
        else:
            for half in ("from", "to"):
                v = date.get(half)
                if v and (not isinstance(v, str) or not DATE_RE.match(v)):
                    err(f"source.date.{half}", "bad_date", "dates are YYYY-MM-DD")
            frm, to = date.get("from"), date.get("to")
            if isinstance(frm, str) and isinstance(to, str) and frm and to and frm > to:
                err("source.date", "bad_date", "date.from is after date.to")
    duration = source.get("duration")
    if duration is not None:
        if not isinstance(duration, dict) or not duration:
            err("source.duration", "bad_duration", "duration needs {min, max} seconds")
        else:
            for half in ("min", "max"):
                v = duration.get(half)
                if v is None:
                    continue
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    err(f"source.duration.{half}", "bad_duration",
                        f"{half} must be a non-negative integer of seconds")
            lo, hi = duration.get("min"), duration.get("max")
            if isinstance(lo, int) and isinstance(hi, int) and not isinstance(lo, bool) \
                    and not isinstance(hi, bool) and hi < lo:
                err("source.duration", "bad_duration", "duration.max is below min")
    created = source.get("createdAt")
    if created is not None:
        days = created.get("withinDays") if isinstance(created, dict) else None
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            err("source.createdAt", "bad_created_at",
                "createdAt needs {withinDays: >= 1}")
    q = source.get("q")
    if q is not None and (not isinstance(q, str) or not q.strip()):
        err("source.q", "bad_query", "q must be a non-empty text query")
    if kind == "criteria":
        has_include = any(ids(source.get(f)) for f in
                          ("tags", "tagsAny", "performers", "performersAny",
                           "studios", "studiosAny"))
        has_meta = any(source.get(k) for k in ("date", "duration", "createdAt")) \
            or bool((source.get("q") or "").strip() if isinstance(source.get("q"), str) else False)
        has_exclude_only = any(ids(source.get(f)) for f in
                               ("excludeTags", "excludePerformers", "excludeStudios"))
        if not has_include and not has_meta:
            if has_exclude_only:
                err("source", "empty_rules",
                    "only exclusions are set — nothing would ever match; "
                    "add at least one include rule or metadata rule")
            else:
                err("source", "empty_rules",
                    "no rules set — add at least one rule for this pool")
    return errors


def has_membership(source: dict) -> bool:
    """True when the source constrains membership at all (any facet)."""
    kind = source.get("type")
    if kind in ("tag", "performer", "studio", "savedFilter"):
        return True
    if kind in ("filter", "criteria"):
        return any(ids(source.get(f)) for f in _ID_FACETS) or any(
            source.get(k) for k in ("date", "duration", "createdAt")) \
            or bool(isinstance(source.get("q"), str) and source.get("q").strip())
    return False


def build_scene_filter(source: dict, object_filter: dict | None = None) -> dict:
    """Project any supported source into a ``SceneFilterType`` variable.

    Legacy shapes delegate to the proven ``lineup.build_scene_filter``. The
    ``criteria`` shape projects per facet exactly like ``filter`` does, plus
    the new explicit performer/studio exclusions (each exclusion rides its own
    criterion's ``excludes`` — a direct Stash criterion, never a NOT wrapper).
    A criteria source's ANY/ALL per facet maps to INCLUDES vs INCLUDES_ALL.
    """
    kind = source.get("type")
    if kind == "criteria":
        out: dict[str, Any] = {}
        tags_all, tags_any = ids(source.get("tags")), ids(source.get("tagsAny"))
        exclude_tags = ids(source.get("excludeTags"))
        if tags_all or tags_any:
            criterion: dict[str, Any] = {
                "value": tags_all or tags_any,
                "modifier": "INCLUDES_ALL" if tags_all else "INCLUDES",
                "depth": -1,
            }
            if exclude_tags:
                criterion["excludes"] = exclude_tags
            out["tags"] = criterion
        for facet, any_facet, modifier_key, depth, exclude_key in (
            ("performers", "performersAny", "performers", None, "excludePerformers"),
            ("studios", "studiosAny", "studios", -1, "excludeStudios"),
        ):
            all_ids, any_ids = ids(source.get(facet)), ids(source.get(any_facet))
            if all_ids and any_ids:
                raise ValueError(f"criteria source cannot set both {facet} and {any_facet}")
            if not (all_ids or any_ids):
                continue
            criterion = {
                "value": all_ids or any_ids,
                "modifier": "INCLUDES_ALL" if all_ids else "INCLUDES",
            }
            if depth is not None:
                criterion["depth"] = depth
            excludes = ids(source.get(exclude_key))
            if excludes:
                criterion["excludes"] = excludes
            out[modifier_key] = criterion
        date = source.get("date")
        if isinstance(date, dict):
            out["date"] = {"value": str(date.get("from") or ""),
                           "value2": str(date.get("to") or ""),
                           "modifier": "BETWEEN"}
        duration = source.get("duration")
        if isinstance(duration, dict):
            lo = int(duration.get("min") or 0)
            hi = duration.get("max")
            out["duration"] = {
                "value": lo,
                "value2": int(hi) if hi else INT_MAX,
                "modifier": "BETWEEN",
            }
        created = source.get("createdAt")
        if isinstance(created, dict):
            out["created_at"] = {
                "value": created_cutoff(int(created["withinDays"])),
                "modifier": "GREATER_THAN",
            }
        q = source.get("q")
        return out
    if kind == "filter":
        # The compiled tier's shape, including the generalised exclusions when
        # a migrated/authored row carries them.
        projected = build_scene_filter({**source, "type": "criteria"}, object_filter)
        return projected
    return _lineup_scene_filter(source, object_filter)


def _lineup_scene_filter(source: dict, object_filter: dict | None) -> dict:
    from justwatch import lineup
    return lineup.build_scene_filter(source, object_filter)


def source_signature(source: dict) -> str:
    """Stable identity of a source's MEMBERSHIP (what invalidates rotations,
    counts, and publications). Dynamic recency contributes its authored
    ``withinDays``, never the resolved cutoff — the epoch moves, the rule
    does not. Cosmetic metadata (name/color/glyph/group) never reaches here.
    """
    canonical = normalize(source)
    return hashlib.sha1(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8"),
    ).hexdigest()[:12]


def rotation_signature(source: dict, sort: str, seed: int, size: int,
                       today: _dt.date | None = None) -> str:
    """The full rotation-ordering identity (membership + order + epoch)."""
    from justwatch.lineup import rotation_version
    return rotation_version(source, sort, int(seed), size, today)


def summarize(source: dict, resolve_name=None) -> list[str]:
    """Plain-language membership sentences (server-side mirror of the editor's
    summary, used by ValidateChannelChanges effect output). ``resolve_name``
    optionally maps (kind, id) -> display name."""
    def name(kind: str, i: str) -> str:
        return resolve_name(kind, i) if resolve_name else f"#{i}"

    def names(kind: str, listing: list[str], cap: int = 3) -> str:
        shown = [name(kind, i) for i in listing[:cap]]
        rest = len(listing) - len(shown)
        return ", ".join(shown) + (f" +{rest}" if rest > 0 else "")

    kind = source.get("type")
    if kind == "savedFilter":
        return ["Airs from the linked saved search, verbatim (including its text query)."]
    if kind == "tag":
        listing = ids(source.get("ids")) or ([str(source["id"])] if source.get("id") else [])
        return [f"Scenes tagged with any of: {names('tag', listing)}."]
    if kind == "performer":
        return [f"Scenes featuring {name('performer', str(source.get('id')))}. No other rules."]
    if kind == "studio":
        return [f"Scenes from studio {name('studio', str(source.get('id')))} (sub-studios included). No other rules."]
    if kind not in ("filter", "criteria"):
        return [f"Airs from a {kind or 'unknown'} source."]
    lines = []
    rows = 0
    if ids(source.get("tags")) or ids(source.get("tagsAny")) or ids(source.get("excludeTags")):
        rows += 1
        parts = []
        if ids(source.get("tags")):
            parts.append(f"tagged with ALL of: {names('tag', ids(source['tags']))}")
        if ids(source.get("tagsAny")):
            parts.append(f"tagged with any of: {names('tag', ids(source['tagsAny']))}")
        if ids(source.get("excludeTags")):
            parts.append(f"without any of: {names('tag', ids(source['excludeTags']))}")
        lines.append(f"{rows}. Scenes " + ", ".join(parts) + " (sub-tags included).")
    if ids(source.get("performers")):
        rows += 1
        lines.append(f"{rows}. Featuring ALL of: {names('performer', ids(source['performers']))}.")
    if ids(source.get("performersAny")):
        rows += 1
        lines.append(f"{rows}. Featuring any of: {names('performer', ids(source['performersAny']))}.")
    if ids(source.get("excludePerformers")):
        rows += 1
        lines.append(f"{rows}. NOT featuring: {names('performer', ids(source['excludePerformers']))}.")
    if ids(source.get("studios")):
        rows += 1
        lines.append(f"{rows}. From studios (sub-studios included): {names('studio', ids(source['studios']))}.")
    if ids(source.get("studiosAny")):
        rows += 1
        lines.append(f"{rows}. From any of: {names('studio', ids(source['studiosAny']))}.")
    if ids(source.get("excludeStudios")):
        rows += 1
        lines.append(f"{rows}. NOT from: {names('studio', ids(source['excludeStudios']))}.")
    date = source.get("date")
    if isinstance(date, dict) and (date.get("from") or date.get("to")):
        rows += 1
        lines.append(f"{rows}. Dated {date.get('from') or 'the beginning'} to {date.get('to') or 'today'}.")
    duration = source.get("duration")
    if isinstance(duration, dict):
        rows += 1
        bounds = []
        if duration.get("min"):
            bounds.append(f"at least {int(duration['min']) // 60} min")
        if duration.get("max"):
            bounds.append(f"at most {int(duration['max']) // 60} min")
        lines.append(f"{rows}. Running " + " and ".join(bounds) + ".")
    created = source.get("createdAt")
    if isinstance(created, dict) and created.get("withinDays"):
        rows += 1
        lines.append(f"{rows}. Added to the library within the last {created['withinDays']} days (moves with the calendar).")
    if isinstance(source.get("q"), str) and source.get("q", "").strip():
        rows += 1
        lines.append(f"{rows}. Matching the text search “{source['q'].strip()}”.")
    if not lines:
        lines.append("No rules yet — the pool is empty until at least one rule is on.")
    return lines
