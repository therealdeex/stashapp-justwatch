"""Pure deterministic planner for channel organization (arrangements).

:func:`plan` consumes the COMMITTED library document plus ONE intent (and
optionally narrow temp-row overlays for pending creations) and returns the
complete preview response body: the final number map as explicit changes,
group changes, capacity accounting, warnings, typed errors, the shared
number-resolution ``choices`` block, and — when the plan is valid and does
something — the submit-ready ``packet`` object (``{expectedRevision, ops}``;
``group.put`` → ``channel.create`` skeletons → ``channels.move`` →
``channels.renumber``, in that fixed order inside ``ops``).

Purity contract: no I/O, no clocks, no Stash queries, no dependence on dict
iteration order — every collection is sorted explicitly, so the same document
plus the same request yields a byte-identical response forever. The document
is never mutated. Bands are immutable per kind (``ch`` 1-99, ``net`` 100-899)
and archived/paused/disabled rows occupy their numbers like any other row:
filtering never frees capacity.

Selection semantics: moving records are removed from occupancy FIRST (their
old slots are available to the plan), and an intent's own subjects are
distinguished from displaced bystanders by the ``selected`` flag. Reasons:
``direct`` (uncontested placement of an intent's own subjects, including
block members), ``insert-shift-up``/``insert-shift-down``, ``block-displaced``
(bystander pushed by a block), ``range-pack``, ``exclusive-outside-relocation``,
``shift-interval``, ``relocate``, ``swap``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from justwatch import contract, library

BANDS = library.BANDS
MAX_NAME_LEN = library.MAX_NAME_LEN
MAX_LIBRARY_CHANNELS = library.MAX_LIBRARY_CHANNELS

#: Overlays are temp refs: opaque, short, and never shaped like a real id.
_TEMP_TOKEN_PREFIX = "t:"
_REAL_ID_RE = re.compile(r"^(?:ch|net)_[0-9a-f]{8}$")
_GRP_ID_RE = library.GRP_ID_RE

_CHOICES_LIST_DEFAULT = 5
_CHOICES_LIST_MAX = 25


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _err(errors: list, path: str, code: str, message: str) -> None:
    errors.append({"path": path, "code": code, "message": message})


def _bounded(names: list, limit: int = 5) -> str:
    shown = ", ".join(str(n) for n in names[:limit])
    extra = len(names) - limit
    return shown + (f" (+{extra} more)" if extra > 0 else "")


# ---------------------------------------------------------------------------
# Rows (committed channels + temp overlays)
# ---------------------------------------------------------------------------


def _rows(doc: dict, overlays: Any, errors: list) -> dict:
    """Unified row map keyed by token: channel id, or ``t:<tempRef>``."""
    rows: dict[str, dict] = {}
    for c in doc["channels"]:
        rows[c["id"]] = {
            "token": c["id"], "ref": c["id"], "tempRef": None,
            "kind": c["kind"], "number": c["number"], "name": c["name"],
            "groupId": c["groupId"], "sort": c.get("sort"),
            "archived": bool(c.get("archived")),
            "paused": bool(c.get("paused")),
            "enabled": bool(c.get("enabled", True)),
        }
    seen_refs: set[str] = set()
    for i, ov in enumerate(overlays or []):
        path = f"channels[{i}]"
        if not isinstance(ov, dict):
            _err(errors, path, "bad_intent", "overlay entries must be JSON objects")
            continue
        ref = ov.get("tempRef")
        if not isinstance(ref, str) or not (1 <= len(ref.strip()) <= 64) \
                or _REAL_ID_RE.match(ref):
            _err(errors, f"{path}.tempRef", "bad_temp_ref",
                 "tempRef must be a 1-64 char opaque ref and never look like a "
                 "real channel id (ch_…/net_…)")
            continue
        if ref in seen_refs:
            _err(errors, f"{path}.tempRef", "duplicate_temp_ref",
                 f"tempRef {ref!r} appears twice in this request")
            continue
        seen_refs.add(ref)
        kind = ov.get("kind")
        if kind not in BANDS:
            _err(errors, f"{path}.kind", "bad_intent",
                 "overlay kind must be 'ch' or 'net' (the band is fixed even "
                 "for uncommitted rows)")
            continue
        name = ov.get("name")
        if name is not None and (not isinstance(name, str)
                                 or not (1 <= len(name.strip()) <= MAX_NAME_LEN)):
            _err(errors, f"{path}.name", "bad_name",
                 f"overlay name must be 1-{MAX_NAME_LEN} characters when present")
            name = None
        group_id = ov.get("groupId")
        if group_id is not None and group_id not in {g["id"] for g in doc["groups"]}:
            _err(errors, f"{path}.groupId", "unknown_group",
                 f"no group {group_id!r}")
            continue
        sort = ov.get("sort")
        if sort is not None and not isinstance(sort, str):
            _err(errors, f"{path}.sort", "bad_intent", "overlay sort must be a string")
            continue
        rows[f"{_TEMP_TOKEN_PREFIX}{ref}"] = {
            "token": f"{_TEMP_TOKEN_PREFIX}{ref}", "ref": None, "tempRef": ref,
            "kind": kind, "number": None,
            "name": name.strip() if isinstance(name, str) else f"new channel {ref}",
            "groupId": group_id, "sort": sort,
            "archived": False, "paused": False, "enabled": True,
        }
    return rows


def _resolve_selection(intent: dict, rows: dict, errors: list, *, required: bool):
    """Selection rows for intents that take channelIds/tempRefs. None on error."""
    sel: list[dict] = []
    ids = intent.get("channelIds")
    temps = intent.get("tempRefs")
    if ids is not None:
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            _err(errors, "intent.channelIds", "bad_intent",
                 "channelIds must be a list of channel ids")
            return None
        for cid in dict.fromkeys(ids):
            row = rows.get(cid)
            if row is None or row["tempRef"] is not None:
                _err(errors, "intent.channelIds", "unknown_channel",
                     f"no such channel {cid!r}")
            else:
                sel.append(row)
    if temps is not None:
        if not isinstance(temps, list) or any(not isinstance(x, str) for x in temps):
            _err(errors, "intent.tempRefs", "bad_intent",
                 "tempRefs must be a list of temp refs")
            return None
        for ref in dict.fromkeys(temps):
            row = rows.get(f"{_TEMP_TOKEN_PREFIX}{ref}")
            if row is None:
                _err(errors, "intent.tempRefs", "unknown_temp_ref",
                     f"no overlay row for tempRef {ref!r}")
            else:
                sel.append(row)
    if errors:
        return None
    if required and not sel:
        _err(errors, "intent", "empty_selection",
             "select at least one channel (channelIds/tempRefs)")
        return None
    return sel


def _order_rows(sel: list, order: str) -> list:
    """Deterministic ordering. ``number``: ascending current number, temp rows
    after existing ones. ``name``: casefolded name, tie-broken by token
    (channel id, or ``t:<tempRef>`` which cannot collide with a real id)."""
    if order == "name":
        return sorted(sel, key=lambda r: (r["name"].strip().casefold(), r["token"]))
    return sorted(sel, key=lambda r: (0, r["number"], r["token"])
                  if r["number"] is not None else (1, 0, r["token"]))


# ---------------------------------------------------------------------------
# Ranges, occupancy, capacity
# ---------------------------------------------------------------------------


def _validate_range(raw: Any, path: str, errors: list):
    if not isinstance(raw, dict) or set(raw) - {"start", "end"} \
            or not _is_int(raw.get("start")) or not _is_int(raw.get("end")):
        _err(errors, path, "bad_range", "range must be {\"start\": int, \"end\": int}")
        return None
    start, end = raw["start"], raw["end"]
    if start > end:
        _err(errors, path, "bad_range", f"range start {start} exceeds end {end}")
        return None
    if start < 1 or end > MAX_LIBRARY_CHANNELS:
        _err(errors, path, "bad_range",
             f"range {start}-{end} sits outside the number universe 1-{MAX_LIBRARY_CHANNELS}")
        return None
    if start <= 99 < end:
        _err(errors, path, "cross_band",
             "range spans the My Channels (1-99) / networks (100-899) boundary; "
             "split it per band")
        return None
    return (start, end)


def _band_kind(start: int, end: int) -> str:
    return "ch" if end <= 99 else "net"


def _occupancy(rows: dict, *, exclude: set | None = None) -> dict:
    """number → token over every committed row (archived/paused/disabled
    included) and every PLACED overlay row."""
    skip = exclude or set()
    return {r["number"]: r["token"] for r in rows.values()
            if r["token"] not in skip and r["number"] is not None}


def _free_numbers(rows: dict, kind: str, *, exclude: set | None = None) -> list:
    lo, hi = BANDS[kind]
    occ = _occupancy(rows, exclude=exclude)
    return [n for n in range(lo, hi + 1) if n not in occ]


def _band_capacity(rows: dict, kind: str, *, needed: int = 0, shortfall: int = 0,
                   rng: dict | None = None, outside: dict | None = None) -> dict:
    lo, hi = BANDS[kind]
    committed = sum(1 for r in rows.values()
                    if r["kind"] == kind and r["tempRef"] is None)
    total = hi - lo + 1
    return {"kind": kind, "bandStart": lo, "bandEnd": hi,
            "totalSlots": total, "occupiedSlots": committed,
            "freeSlots": total - committed,
            "neededSlots": needed, "shortfall": shortfall,
            "range": rng, "outside": outside}


def _range_block(rows: dict, kind: str, start: int, end: int, *,
                 exclude: set | None = None) -> dict:
    occ = _occupancy(rows, exclude=exclude)
    insiders = sum(1 for n in occ if start <= n <= end and rows[occ[n]]["kind"] == kind)
    total = end - start + 1
    return {"start": start, "end": end, "totalSlots": total,
            "occupiedSlots": insiders, "freeSlots": total - insiders,
            "outsiderSlots": insiders}


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def _new_out() -> dict:
    return {"moves": [], "groups": [], "created": [], "new_groups": {},
            "capacity": None, "suggestions": None, "choices": None}


def _move(out: dict, row: dict, to: int, reason: str, selected: bool) -> None:
    out["moves"].append({"row": row, "frm": row["number"], "to": to,
                         "reason": reason, "selected": selected})


def _group(out: dict, row: dict, to: str) -> None:
    out["groups"].append({"row": row, "frm": row["groupId"], "to": to})


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------


def _intent_assign_group(doc, rows, intent, errors, out) -> None:
    sel = _resolve_selection(intent, rows, errors, required=True)
    if sel is None:
        return
    gid = intent.get("groupId")
    create = intent.get("createGroup")
    if (gid is None) == (create is None):
        _err(errors, "intent", "bad_intent",
             "assign_group needs exactly one of groupId or createGroup")
        return
    groups = {g["id"]: g for g in doc["groups"]}
    if gid is not None:
        if not isinstance(gid, str) or gid not in groups:
            _err(errors, "intent.groupId", "unknown_group", f"no group {gid!r}")
            return
        target = gid
    else:
        target = _plan_new_group(doc, create, groups, out, errors)
        if target is None:
            return
    for row in sel:
        _group(out, row, target)
        if row["tempRef"] is not None:
            out["created"].append({"tempRef": row["tempRef"], "number": None,
                                   "groupId": target, "kind": row["kind"],
                                   "name": row["name"], "placed": False})


def _plan_new_group(doc, create: Any, groups: dict, out: dict, errors: list):
    if not isinstance(create, dict):
        _err(errors, "intent.createGroup", "bad_intent",
             "createGroup must be an object {name, position?, id?}")
        return None
    name = create.get("name")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= MAX_NAME_LEN):
        _err(errors, "intent.createGroup.name", "bad_name",
             f"group name must be 1-{MAX_NAME_LEN} characters")
        return None
    norm = name.strip().casefold()
    pending = {g["name"].strip().casefold() for g in out["new_groups"].values()}
    if any(g["name"].strip().casefold() == norm for g in groups.values()) \
            or norm in pending:
        _err(errors, "intent.createGroup.name", "duplicate_group",
             f"a group named {name.strip()!r} exists")
        return None
    position = create.get("position")
    if position is not None and not (_is_int(position) and position >= 1):
        _err(errors, "intent.createGroup.position", "bad_intent",
             "position must be a positive integer")
        return None
    explicit = create.get("id")
    if explicit is not None:
        if not isinstance(explicit, str) or not _GRP_ID_RE.match(explicit):
            _err(errors, "intent.createGroup.id", "bad_intent",
                 "createGroup.id must look like grp_…")
            return None
        if explicit in groups:
            _err(errors, "intent.createGroup.id", "duplicate_group",
                 f"group id {explicit!r} already exists")
            return None
        target = explicit
    else:
        target = _derive_group_id(doc, name, groups, out)
        if target is None:
            _err(errors, "intent.createGroup.name", "duplicate_group",
                 f"could not derive a free group id for {name.strip()!r}")
            return None
    if position is None:
        position = max((g["position"] for g in groups.values()), default=0) + 1
    out["new_groups"][target] = {"name": name.strip(), "position": position}
    return target


def _derive_group_id(doc: dict, name: str, groups: dict, out: dict) -> str | None:
    """Deterministic id for a created group (the packet must reference it).
    Same library + same name → same id, forever."""
    digest = hashlib.sha256(
        f"{doc['libraryId']}\n{name.strip().casefold()}".encode("utf-8")).hexdigest()
    taken = set(groups) | set(out["new_groups"])
    for i in range(16):
        candidate = f"grp_{digest[:12]}" if i == 0 else f"grp_{digest[:11]}{format(i, 'x')}"
        if candidate not in taken:
            return candidate
    return None


def _intent_arrange_range(doc, rows, intent, errors, out) -> None:
    rng = _validate_range(intent.get("range"), "intent.range", errors)
    if rng is None:
        return
    start, end = rng
    kind = _band_kind(start, end)
    sel = _resolve_selection(intent, rows, errors, required=True)
    if sel is None:
        return
    offenders = [r["ref"] or r["tempRef"] for r in sel if r["kind"] != kind]
    if offenders:
        _err(errors, "intent", "band_mixed",
             f"these channels are not {kind} channels and cannot enter "
             f"{start}-{end}: {_bounded(offenders)}; split the arrangement "
             "per band explicitly")
        return
    order = intent.get("order") or "number"
    if order not in ("number", "name"):
        _err(errors, "intent.order", "bad_intent",
             "order must be 'number' or 'name'")
        return
    strategy = intent.get("strategy") or "useAvailable"
    if strategy not in ("useAvailable", "exclusive"):
        _err(errors, "intent.strategy", "bad_intent",
             "strategy must be 'useAvailable' or 'exclusive'")
        return
    sel_tokens = {r["token"] for r in sel}
    occ = _occupancy(rows, exclude=sel_tokens)
    outsiders = sorted(
        (rows[t] for t in {tok for n, tok in occ.items() if start <= n <= end}),
        key=lambda r: r["number"])
    range_block = _range_block(rows, kind, start, end, exclude=sel_tokens)
    range_block["outsiderSlots"] = len(outsiders)
    outside_block = None
    if strategy == "exclusive":
        outside = _validate_range(intent.get("outside"), "intent.outside", errors)
        if outside is None:
            return
        o_start, o_end = outside
        if _band_kind(o_start, o_end) != kind:
            _err(errors, "intent.outside", "bad_range",
                 f"the outside interval must sit inside the {kind} band "
                 f"{BANDS[kind][0]}-{BANDS[kind][1]}")
            return
        if not (o_end < start or o_start > end):
            _err(errors, "intent.outside", "range_overlap",
                 f"the outside interval {o_start}-{o_end} overlaps the "
                 f"exclusive range {start}-{end}")
            return
        free_outside = [n for n in range(o_start, o_end + 1) if n not in occ]
        outside_block = {"start": o_start, "end": o_end,
                         "totalSlots": o_end - o_start + 1,
                         "occupiedSlots": (o_end - o_start + 1) - len(free_outside),
                         "freeSlots": len(free_outside),
                         "neededSlots": len(outsiders)}
        if len(free_outside) < len(outsiders):
            _err(errors, "intent.outside", "no_capacity",
                 f"{len(outsiders)} outsider channel(s) must move but "
                 f"{o_start}-{o_end} has only {len(free_outside)} free numbers")
            out["capacity"] = _band_capacity(rows, kind, needed=len(sel),
                                             shortfall=max(0, len(sel) - (end - start + 1)),
                                             rng=range_block, outside=outside_block)
            return
        for row, dest in zip(outsiders, free_outside):
            _move(out, row, dest, "exclusive-outside-relocation", selected=False)
        if len(sel) > end - start + 1:
            _err(errors, "intent.range", "no_capacity",
                 f"{len(sel)} channels do not fit in {start}-{end} "
                 f"({end - start + 1} slots)")
            out["capacity"] = _band_capacity(rows, kind, needed=len(sel),
                                             shortfall=len(sel) - (end - start + 1),
                                             rng=range_block, outside=outside_block)
            return
        for i, row in enumerate(_order_rows(sel, order)):
            _move(out, row, start + i, "range-pack", selected=True)
    else:
        free_positions = [n for n in range(start, end + 1) if n not in occ]
        if len(free_positions) < len(sel):
            shortfall = len(sel) - len(free_positions)
            _err(errors, "intent.range", "no_capacity",
                 f"{len(sel)} channels need {len(sel)} free numbers in "
                 f"{start}-{end}; only {len(free_positions)} are free "
                 f"({len(outsiders)} outsider(s) hold the rest)")
            out["capacity"] = _band_capacity(rows, kind, needed=len(sel),
                                             shortfall=shortfall,
                                             rng=range_block)
            return
        for row, dest in zip(_order_rows(sel, order), free_positions):
            _move(out, row, dest, "range-pack", selected=True)
    out["capacity"] = _band_capacity(rows, kind, needed=len(sel),
                                     rng=range_block, outside=outside_block)


def _intent_move_block(doc, rows, intent, errors, out) -> None:
    sel = _resolve_selection(intent, rows, errors, required=True)
    if sel is None:
        return
    kind = sel[0]["kind"]
    offenders = [r["ref"] or r["tempRef"] for r in sel if r["kind"] != kind]
    if offenders:
        _err(errors, "intent", "band_mixed",
             f"the block mixes namespaces: {_bounded(offenders)} are not "
             f"{kind} channels; move one block per band")
        return
    start = intent.get("start")
    if not _is_int(start):
        _err(errors, "intent.start", "bad_number", "start must be a real integer")
        return
    lo, hi = BANDS[kind]
    count = len(sel)
    if start < lo or start + count - 1 > hi:
        _err(errors, "intent.start", "cross_band",
             f"a block of {count} channels starting at {start} does not fit "
             f"the {kind} band {lo}-{hi}")
        return
    sel_tokens = {r["token"] for r in sel}
    occ = _occupancy(rows, exclude=sel_tokens)
    for offset, row in enumerate(sel):
        _move(out, row, start + offset, "direct", selected=True)
        if row["tempRef"] is not None:
            out["created"].append({"tempRef": row["tempRef"],
                                   "number": start + offset,
                                   "groupId": row["groupId"],
                                   "kind": row["kind"], "name": row["name"],
                                   "placed": True})
    span = set(range(start, start + count))
    displaced = sorted(
        (rows[t] for t in {tok for n, tok in occ.items() if n in span}),
        key=lambda r: r["number"])
    available = [n for n in range(start, hi + 1) if n not in span and n not in occ]
    if len(available) < len(displaced):
        _err(errors, "intent.start", "no_capacity",
             f"{len(displaced)} displaced channel(s) do not fit between "
             f"{start} and {hi} ({len(available)} free slots above the block)")
        out["capacity"] = _band_capacity(rows, kind, needed=count,
                                         shortfall=len(displaced) - len(available))
        return
    for row, dest in zip(displaced, available):
        _move(out, row, dest, "block-displaced", selected=False)
    out["capacity"] = _band_capacity(rows, kind, needed=count)


def _insert_choices(rows: dict, mover: dict, number: int, count: int) -> dict:
    """The shared number-resolution sheet's choice block: every legal
    alternative for ONE insert, computed against occupancy with the mover's
    own slot removed."""
    kind = mover["kind"]
    lo, hi = BANDS[kind]
    occ = _occupancy(rows, exclude={mover["token"]})
    free = [n for n in range(lo, hi + 1) if n not in occ]
    free_set = set(free)
    target_free = number in free_set
    ups = [n for n in free if n >= number]
    downs = [n for n in reversed(free) if n < number]
    free_block = {
        "nextHigher": ups[0] if ups else None,
        "nearest": min(free, key=lambda n: (abs(n - number), n)) if free else None,
        "firstFree": free[0] if free else None,
        "list": (ups + downs)[:count],
    }
    if mover["tempRef"] is not None:
        swap = {"available": False,
                "reason": "a new channel has no original slot to swap into"}
    elif target_free:
        swap = {"available": False,
                "reason": f"number {number} is free; nothing to swap with"}
    else:
        holder = rows[occ[number]]
        swap = {"available": True, "with": holder["ref"],
                "withName": holder["name"], "withNumber": number}
    if target_free:
        shift_up = {"available": False,
                    "reason": f"number {number} is free; no shift needed"}
        shift_down = {"available": False,
                      "reason": f"number {number} is free; no shift needed"}
    else:
        above = [n for n in free if n > number]
        below = [n for n in free if n < number]
        shift_up = {"available": bool(above),
                    "firstFree": above[0] if above else None,
                    "movedCount": (above[0] - number) if above else None}
        if not above:
            shift_up["reason"] = f"No free number above {hi}."
        shift_down = {"available": bool(below),
                      "firstFree": below[-1] if below else None,
                      "movedCount": (number - below[-1]) if below else None}
        if not below:
            shift_down["reason"] = f"No free number below {lo}."
    if target_free:
        relocate = {"available": False,
                    "reason": f"number {number} is free; no occupant to relocate"}
    else:
        relocate = {"available": bool(free)}
        if not free:
            relocate["reason"] = f"band {lo}-{hi} is full"
    return {"free": free_block, "swap": swap, "shiftUp": shift_up,
            "shiftDown": shift_down, "relocate": relocate}


def _intent_insert(doc, rows, intent, errors, out, *, explain: bool) -> None:
    ref = intent.get("channelId")
    temp = intent.get("tempRef")
    if (ref is None) == (temp is None):
        _err(errors, "intent", "bad_intent",
             "insert needs exactly one of channelId or tempRef")
        return
    if ref is not None:
        mover = rows.get(ref) if isinstance(ref, str) else None
        if mover is None or mover["tempRef"] is not None:
            _err(errors, "intent.channelId", "unknown_channel",
                 f"no such channel {ref!r}")
            return
    else:
        mover = rows.get(f"{_TEMP_TOKEN_PREFIX}{temp}") if isinstance(temp, str) else None
        if mover is None:
            _err(errors, "intent.tempRef", "unknown_temp_ref",
                 f"no overlay row for tempRef {temp!r}")
            return
    number = intent.get("number")
    if not _is_int(number):
        _err(errors, "intent.number", "bad_number", "number must be a real integer")
        return
    direction = intent.get("direction") or "up"
    if direction not in ("up", "down"):
        _err(errors, "intent.direction", "bad_intent",
             "direction must be 'up' or 'down'")
        return
    kind = mover["kind"]
    lo, hi = BANDS[kind]
    if not (lo <= number <= hi):
        _err(errors, "intent.number", "cross_band",
             f"a {kind} channel must stay within {lo}-{hi}")
        return
    if explain:
        out["choices"] = _insert_choices(rows, mover, number, _CHOICES_LIST_DEFAULT)
    occ = _occupancy(rows, exclude={mover["token"]})
    out["capacity"] = _band_capacity(rows, kind, needed=1)
    if mover["number"] == number:
        return  # already there: valid no-op
    if number not in occ:
        _move(out, mover, number, "direct", selected=True)
        if mover["tempRef"] is not None:
            out["created"].append({"tempRef": mover["tempRef"], "number": number,
                                   "groupId": mover["groupId"], "kind": kind,
                                   "name": mover["name"], "placed": True})
        return
    if direction == "up":
        first_free = next((n for n in range(number, hi + 1) if n not in occ), None)
        if first_free is None:
            _err(errors, "intent.number", "no_capacity",
                 f"No free number above {hi}.")
            out["capacity"]["shortfall"] = 1
            return
        for n in range(first_free - 1, number - 1, -1):
            _move(out, rows[occ[n]], n + 1, "insert-shift-up", selected=False)
        _move(out, mover, number, "insert-shift-up", selected=True)
    else:
        first_free = next((n for n in range(number, lo - 1, -1) if n not in occ), None)
        if first_free is None:
            _err(errors, "intent.number", "no_capacity",
                 f"No free number below {lo}.")
            out["capacity"]["shortfall"] = 1
            return
        for n in range(first_free + 1, number + 1):
            _move(out, rows[occ[n]], n - 1, "insert-shift-down", selected=False)
        _move(out, mover, number, "insert-shift-down", selected=True)
    if mover["tempRef"] is not None:
        out["created"].append({"tempRef": mover["tempRef"], "number": number,
                               "groupId": mover["groupId"], "kind": kind,
                               "name": mover["name"], "placed": True})


def _intent_shift_interval(doc, rows, intent, errors, out) -> None:
    rng = _validate_range(intent.get("range"), "intent.range", errors)
    if rng is None:
        return
    start, end = rng
    offset = intent.get("offset")
    if not _is_int(offset):
        _err(errors, "intent.offset", "bad_number", "offset must be a real integer")
        return
    kind = _band_kind(start, end)
    if offset == 0:
        out["capacity"] = _band_capacity(rows, kind)
        return  # nothing changes: valid no-op
    occ = _occupancy(rows)
    movers = sorted(
        (r for r in rows.values()
         if r["number"] is not None and start <= r["number"] <= end),
        key=lambda r: r["number"])
    out["capacity"] = _band_capacity(rows, kind, needed=len(movers))
    if not movers:
        return  # the interval is empty: valid no-op
    moving = {r["token"] for r in movers}
    for row in movers:
        dest = row["number"] + offset
        lo, hi = BANDS[row["kind"]]
        if not (lo <= dest <= hi):
            _err(errors, "intent.offset", "cross_band",
                 f"{row['ref']} would move {row['number']}→{dest}, outside its "
                 f"band {lo}-{hi}; shift a smaller interval or per band")
            return
    conflicts = []
    for row in movers:
        dest = row["number"] + offset
        holder = occ.get(dest)
        if holder is not None and holder not in moving:
            conflicts.append((dest, rows[holder]))
    if conflicts:
        detail = ", ".join(f"{n} (held by {r['name']})" for n, r in conflicts[:5])
        more = len(conflicts) - 5
        _err(errors, "intent.offset", "range_overlap",
             f"the shift would land on channels this intent does not move: "
             f"{detail}" + (f" (+{more} more)" if more > 0 else "")
             + "; relocate the blockers explicitly or pick another action")
        return
    for row in movers:
        _move(out, row, row["number"] + offset, "shift-interval", selected=True)


def _intent_relocate_occupant(doc, rows, intent, errors, out) -> None:
    cid = intent.get("channelId")
    row = rows.get(cid) if isinstance(cid, str) else None
    if row is None or row["tempRef"] is not None:
        _err(errors, "intent.channelId", "unknown_channel",
             f"no such channel {cid!r}")
        return
    to = intent.get("to")
    if not _is_int(to):
        _err(errors, "intent.to", "bad_number", "to must be a real integer")
        return
    kind = row["kind"]
    lo, hi = BANDS[kind]
    if not (lo <= to <= hi):
        _err(errors, "intent.to", "cross_band",
             f"a {kind} channel must stay within {lo}-{hi}")
        return
    out["capacity"] = _band_capacity(rows, kind, needed=1)
    if row["number"] == to:
        return  # already there: valid no-op
    occ = _occupancy(rows, exclude={row["token"]})
    holder = occ.get(to)
    if holder is not None:
        blocker = rows[holder]
        _err(errors, "intent.to", "destination_occupied",
             f"channel {to} is held by {blocker['name']} ({blocker['ref']}); "
             "relocate it first or include it in the same arrangement")
        return
    _move(out, row, to, "relocate", selected=True)


def _intent_free_number(doc, rows, intent, errors, out, *, explain: bool) -> None:
    kind = intent.get("kind")
    if kind not in BANDS:
        _err(errors, "intent.kind", "bad_intent", "kind must be 'ch' or 'net'")
        return
    near = intent.get("near")
    if not _is_int(near):
        _err(errors, "intent.near", "bad_number", "near must be a real integer")
        return
    count = intent.get("count")
    if count is None:
        count = _CHOICES_LIST_DEFAULT
    elif not _is_int(count):
        _err(errors, "intent.count", "bad_number", "count must be a real integer")
        return
    count = min(max(count, 1), _CHOICES_LIST_MAX)
    lo, hi = BANDS[kind]
    free = _free_numbers(rows, kind)
    ups = [n for n in free if n >= near]
    downs = [n for n in reversed(free) if n < near]
    suggestions = {
        "nextHigher": ups[0] if ups else None,
        "nearest": min(free, key=lambda n: (abs(n - near), n)) if free else None,
        "firstFree": free[0] if free else None,
        "list": (ups + downs)[:count],
    }
    out["suggestions"] = suggestions
    out["capacity"] = _band_capacity(rows, kind)
    if explain:
        out["choices"] = {"free": dict(suggestions)}


def _intent_swap(doc, rows, intent, errors, out) -> None:
    a = intent.get("a")
    b = intent.get("b")
    row_a = rows.get(a) if isinstance(a, str) else None
    row_b = rows.get(b) if isinstance(b, str) else None
    if row_a is None or row_b is None or row_a["tempRef"] is not None \
            or row_b["tempRef"] is not None:
        _err(errors, "intent.a/b", "unknown_channel",
             "swap needs two existing channel ids")
        return
    if row_a["token"] == row_b["token"]:
        _err(errors, "intent.a/b", "bad_swap", "cannot swap a channel with itself")
        return
    if row_a["kind"] != row_b["kind"]:
        _err(errors, "intent.a/b", "bad_swap", "swaps stay within one number band")
        return
    lo, hi = BANDS[row_a["kind"]]
    out["capacity"] = _band_capacity(rows, row_a["kind"], needed=2)
    _move(out, row_a, row_b["number"], "swap", selected=True)
    _move(out, row_b, row_a["number"], "swap", selected=True)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


_INTENTS = {
    "assign_group": lambda doc, rows, intent, errors, out, explain:
        _intent_assign_group(doc, rows, intent, errors, out),
    "arrange_range": lambda doc, rows, intent, errors, out, explain:
        _intent_arrange_range(doc, rows, intent, errors, out),
    "move_block": lambda doc, rows, intent, errors, out, explain:
        _intent_move_block(doc, rows, intent, errors, out),
    "insert": _intent_insert,
    "shift_interval": lambda doc, rows, intent, errors, out, explain:
        _intent_shift_interval(doc, rows, intent, errors, out),
    "relocate_occupant": lambda doc, rows, intent, errors, out, explain:
        _intent_relocate_occupant(doc, rows, intent, errors, out),
    "free_number": _intent_free_number,
    "swap": lambda doc, rows, intent, errors, out, explain:
        _intent_swap(doc, rows, intent, errors, out),
}


def _warnings(moves: list, groups: list) -> list:
    warns = []
    for flag, code, label in (("archived", "archived_rows_moved", "Archived"),
                              ("paused", "paused_rows_moved", "Paused"),
                              ("enabled", "disabled_rows_moved", "Disabled")):
        if flag == "enabled":
            hits = [m for m in moves if not m["row"]["enabled"]]
        else:
            hits = [m for m in moves if m["row"][flag]]
        if hits:
            detail = _bounded([f"{m['row']['ref']} ({m['frm']}→{m['to']})" for m in hits])
            warns.append({"code": code,
                          "message": f"{label} channels occupy their numbers and "
                                     f"were moved: {detail}"})
    if moves:
        warns.append({"code": "health_numbers_stale",
                      "message": "published health rows keep their last-computed "
                                 "numbers until the next health pass; content "
                                 "health is unaffected"})
    if moves or groups:
        warns.append({"code": "presentation_will_change",
                      "message": "numbers/groups change, so presentation "
                                 "signatures change; clients refresh the "
                                 "directory without resetting playback"})
    return warns


def _packet(out: dict) -> list:
    """The submit-ready op list, in the fixed compile order: group.put →
    channel.create skeletons → channels.move → channels.renumber.
    :func:`plan` wraps this as the ``packet`` object
    ``{"expectedRevision": …, "ops": […]}``. Create entries are SKELETONS
    (kind/number/name/groupId only) — the client merges its full
    pending-create draft (source, color, glyph) before submit."""
    ops: list[dict] = []
    for gid in sorted(out["new_groups"]):
        group = {"id": gid, "name": out["new_groups"][gid]["name"]}
        position = out["new_groups"][gid]["position"]
        if position is not None:
            group["position"] = position
        ops.append({"op": "group.put", "group": group})
    for entry in sorted(out["created"], key=lambda c: c["tempRef"]):
        channel = {"kind": entry["kind"]}
        if entry["placed"]:
            channel["number"] = entry["number"]
        if entry["name"]:
            channel["name"] = entry["name"]
        if entry["groupId"]:
            channel["groupId"] = entry["groupId"]
        ops.append({"op": "channel.create", "tempId": entry["tempRef"],
                    "channel": channel})
    regroups: dict[str, list[str]] = {}
    for change in out["groups"]:
        if change["row"]["tempRef"] is None and change["frm"] != change["to"]:
            regroups.setdefault(change["to"], []).append(change["row"]["ref"])
    for gid in sorted(regroups):
        ops.append({"op": "channels.move", "channelIds": sorted(regroups[gid]),
                    "groupId": gid})
    assignments = sorted(
        (m for m in out["moves"]
         if m["row"]["tempRef"] is None and m["frm"] != m["to"]),
        key=lambda m: (m["to"], m["row"]["ref"]))
    if assignments:
        ops.append({"op": "channels.renumber", "assignments": [
            {"channelId": m["row"]["ref"], "number": m["to"]} for m in assignments]})
    return ops or None


def plan(doc: dict, *, intent: Any, overlays: Any = None, explain: bool = False,
         expected_revision: int | None = None) -> dict:
    """Plan one arrangement against the committed document. Pure; returns the
    preview response body (the handler adds pluginId/contractVersion and the
    echoed correlationToken)."""
    errors: list[dict] = []
    body = {
        "revision": doc["revision"], "libraryId": doc["libraryId"],
        "noop": True, "valid": True,
        "numberChanges": [], "groupChanges": [], "created": [], "displaced": [],
        "capacity": None, "suggestions": None, "choices": None,
        "warnings": [], "errors": errors, "packet": None,
    }
    if expected_revision is not None and expected_revision != doc["revision"]:
        _err(errors, "expectedRevision", "stale_revision",
             f"preview expects revision {expected_revision}, library is at "
             f"{doc['revision']}; reload and replan")
        errors[-1]["currentRevision"] = doc["revision"]
        body["valid"] = False
        return body
    rows = _rows(doc, overlays, errors)
    if errors:
        body["valid"] = False
        return body
    if not isinstance(intent, dict) or not isinstance(intent.get("type"), str):
        _err(errors, "intent", "bad_intent",
             "intent must be an object with a 'type' field")
        body["valid"] = False
        return body
    handler = _INTENTS.get(intent["type"])
    if handler is None:
        _err(errors, "intent.type", "bad_intent",
             f"unknown intent type {intent['type']!r}")
        body["valid"] = False
        return body
    out = _new_out()
    handler(doc, rows, intent, errors, out, explain=bool(explain))
    body["capacity"] = out["capacity"]
    body["suggestions"] = out["suggestions"]
    body["choices"] = out["choices"]
    if errors:
        body["valid"] = False
        return body
    real_moves = [m for m in out["moves"] if m["frm"] != m["to"]]
    real_groups = [g for g in out["groups"] if g["frm"] != g["to"]]
    if not real_moves and not real_groups and not out["created"]:
        return body  # valid no-op: the UI must not spend a revision on it
    body["noop"] = False
    changes = []
    for m in real_moves:
        row = m["row"]
        changes.append({"channelId": row["ref"], "tempRef": row["tempRef"],
                        "from": m["frm"], "to": m["to"],
                        "selected": bool(m["selected"]), "reason": m["reason"]})
    body["numberChanges"] = sorted(changes, key=lambda c: (c["to"], c["channelId"] or ""))
    body["displaced"] = [c for c in body["numberChanges"] if not c["selected"]]
    body["groupChanges"] = sorted(
        ({"channelId": g["row"]["ref"], "tempRef": g["row"]["tempRef"],
          "from": g["frm"], "to": g["to"]} for g in real_groups),
        key=lambda g: g["channelId"] or g["tempRef"] or "")
    body["created"] = [
        {"tempRef": c["tempRef"], "number": c["number"], "groupId": c["groupId"]}
        for c in sorted(out["created"], key=lambda c: c["tempRef"])]
    body["warnings"] = _warnings(real_moves, real_groups)
    body["packet"] = {"expectedRevision": doc["revision"],
                      "ops": _packet(out) or []}
    return body
