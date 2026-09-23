"""Published programming. Only tasks build; sync readers never mutate the schedule.

Each publication carries concrete airing times. A shuffled deck is consumed once
before repeats, with optional soft spacing and weekly spotlight blocks. The index
is fetched in bounded pages by the preparation task, never by a channel tune.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from justwatch import catalog, lineup, snapshots

HOUR = 3_600_000
HORIZON = 72 * HOUR
RETENTION = 48 * HOUR
MAX_INDEX = 100_000
MAX_PROGRAMS = 10_000
MODES = ("fixed", "explore", "discovery")
QUERY = """
query ProgrammingIndex($filter: FindFilterType!, $scene_filter: SceneFilterType!) {
 findScenes(filter: $filter, scene_filter: $scene_filter) {
  count scenes { id title date studio { id name } performers { id }
   files { duration } paths { preview } }
 }
}
"""


def policy(raw):
    raw = raw if isinstance(raw, dict) else {}
    def integer(key, default, low, high):
        value = raw.get(key, default)
        return max(low, min(high, value)) if type(value) is int else default
    return {
        "mode": raw.get("mode") if raw.get("mode") in MODES else "fixed",
        "spacing": integer("spacing", 0, 0, 10),
        "repeatHours": integer("repeatHours", 0, 0, 168),
        "spotlight": raw.get("spotlight") if raw.get("spotlight") in ("studio", "performer") else "none",
        "spotlightDay": integer("spotlightDay", 5, 0, 6),
        "spotlightHour": integer("spotlightHour", 20, 0, 23),
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


_CHANNEL_ID_RE = re.compile(r"(ch|net)_[0-9a-f]{8}")


def path(data_dir, channel_id):
    if not _CHANNEL_ID_RE.fullmatch(channel_id):
        raise ValueError("invalid channel id")
    return Path(data_dir) / "programming" / (channel_id + ".json")


def read(data_dir, channel_id):
    p = path(data_dir, channel_id)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    if data.get("schema") not in (1, 2, 3) or not isinstance(data.get("programs"), list):
        raise ValueError("unreadable published schedule")
    return data


def index_source(client, channel):
    source = channel["source"]
    criteria, q = None, None
    if source["type"] == "savedFilter":
        criteria, q = lineup.resolve_saved_criteria(client, source["id"])
    scene_filter = lineup.build_scene_filter(source, criteria)
    result = {}
    for page in range(1, MAX_INDEX // 250 + 1):
        find = lineup.build_find_filter(channel["sort"], channel["seed"], page, 250, q)
        node = (client.submit(QUERY, {"filter": find, "scene_filter": scene_filter}) or {}).get("findScenes") or {}
        rows = node.get("scenes") or []
        for row in rows:
            item = lineup._scene_item(row, True)
            if item is None or not math.isfinite(item["duration"]) or item["duration"] < 1:
                continue
            item["studioId"] = str((row.get("studio") or {}).get("id") or "")
            item["performerIds"] = [str(p["id"]) for p in row.get("performers") or []]
            result[item["id"]] = item
        if not rows or page * 250 >= int(node.get("count") or 0):
            return list(result.values())
    raise ValueError("source exceeds 100,000 rows; narrow this channel before publishing")


def build(channel, entries, previous=None, now=None):
    """Pure publication builder; deterministic for the same inputs and time."""
    now = int(time.time() * 1000) if now is None else now
    cfg = policy(channel.get("programming"))
    old = copy.deepcopy(previous or {})
    index = {e["id"]: e for e in entries}
    signature = digest([channel["source"], channel["sort"], channel["seed"], cfg])
    changed = old.get("configuration") != signature or bool(set(old.get("indexIds", [])) - set(index))
    # Preserve current airing. Explicit configuration edits replace unpublished
    # future material at its end; routine replenishment never changes future slots.
    retained = [p for p in old.get("programs", []) if p["endEpochMs"] > now - RETENTION]
    programs = [p for p in retained if p["endEpochMs"] <= now][-1000:] + [p for p in retained if p["endEpochMs"] > now]
    if changed:
        # Give clients two cache lifetimes of notice before replacing the future.
        # Preserve whole airings, including a short program about to end.
        boundary = next((p["endEpochMs"] for p in programs if p["endEpochMs"] >= now + 120_000), now)
        programs = [p for p in programs if p["startEpochMs"] < boundary]
    # If the scheduler was offline, finish the deterministic encore currently
    # airing before publishing fresh programming. Never cut a fallback mid-scene.
    if old.get("programs") and old["programs"][-1]["endEpochMs"] <= now:
        block = old["programs"][-50:]
        length = sum(p["endEpochMs"] - p["startEpochMs"] for p in block)
        offset = block[-1]["endEpochMs"] + ((now - block[-1]["endEpochMs"]) // length) * length
        for airing in block:
            end = offset + airing["endEpochMs"] - airing["startEpochMs"]
            if end > now:
                programs.append({**airing, "startEpochMs": offset, "endEpochMs": end,
                    "airingId": digest([channel["id"], offset, airing["item"]["id"]]), "block": "Encore"})
                break
            offset = end
    cursor = max(now, programs[-1]["endEpochMs"] if programs else now)
    counts = old.get("airCounts", {})
    last = old.get("lastScheduled", {})
    deck = [i for i in old.get("deck", []) if i in index] if not changed else []
    pass_number = int(old.get("pass", 0))
    known = set(old.get("indexIds", []))
    # Additions enter the remaining deck without restarting an existing pass.
    if deck:
        deck += sorted(set(index) - known, key=lambda i: digest([channel["seed"], i]))
    warnings = set(old.get("warnings", [])) if not changed else set()
    recent = [p["item"] for p in programs[-10:]]
    spotlight_slots = set(old.get("spotlightSlots", []))
    block_remaining, block_kind, block_id, block_label = 0, "", "", ""
    while index and cursor < now + HORIZON and len(programs) < MAX_PROGRAMS:
        if not deck:
            pass_number += 1
            deck = list(index)
            if channel["sort"] == "shuffle":
                deck.sort(key=lambda i: digest([channel["seed"], pass_number, i]))
            if cfg["mode"] == "discovery":
                deck.sort(key=lambda i: counts.get(i, 0))
        dt = datetime.fromtimestamp(cursor / 1000, timezone.utc)
        slot = dt.strftime("%Y-%m-%d")
        if (cfg["spotlight"] != "none" and dt.weekday() == cfg["spotlightDay"]
                and dt.hour >= cfg["spotlightHour"] and slot not in spotlight_slots):
            spotlight_slots.add(slot)
            groups = {}
            kind = cfg["spotlight"]
            for i in deck:
                keys = [index[i]["studioId"]] if kind == "studio" else index[i]["performerIds"]
                for key in keys:
                    if key:
                        groups.setdefault(key, []).append(i)
            candidates = sorted(k for k, ids in groups.items() if len(ids) >= 2)
            if candidates:
                block_id = candidates[int(digest([channel["seed"], slot]), 16) % len(candidates)]
                block_kind, block_remaining = kind, 2
                block_label = (index[groups[block_id][0]].get("studio") or "Studio spotlight") if kind == "studio" else "Performer double feature"
        candidates = deck
        if block_remaining:
            matches = [i for i in deck if (index[i]["studioId"] == block_id if block_kind == "studio" else block_id in index[i]["performerIds"])]
            if matches:
                candidates = matches
            else:
                block_remaining = 0
        def penalty(i):
            e = index[i]
            cooldown = cursor - last.get(i, -10**18) < cfg["repeatHours"] * HOUR
            neighbors = recent[-cfg["spacing"]:] if cfg["spacing"] else []
            conflict = any((e["studioId"] and e["studioId"] == r.get("studioId")) or
                           set(e["performerIds"]) & set(r.get("performerIds", [])) for r in neighbors)
            return int(cooldown), int(conflict and not block_remaining)
        chosen = next((i for i in candidates if penalty(i) == (0, 0)), None)
        if chosen is None:
            chosen = min(candidates, key=penalty)
            warnings.add("Some repeat or spacing preferences could not be met by this lineup.")
        deck.remove(chosen)
        item = index[chosen]
        end = cursor + max(1000, round(item["duration"] * 1000))
        programs.append({"airingId": digest([channel["id"], cursor, chosen]), "startEpochMs": cursor,
                         "endEpochMs": end, "item": item, "block": block_label if block_remaining else ""})
        if block_remaining:
            block_remaining -= 1
        counts[chosen] = counts.get(chosen, 0) + 1
        last[chosen] = cursor
        recent = (recent + [item])[-10:]
        cursor = end
    public = {"schema": 1, "channelId": channel["id"], "configuration": signature,
              "programs": programs, "preparedThrough": cursor, "sourceTotal": len(index),
              "warnings": sorted(warnings), "mode": cfg["mode"],
              "pass": pass_number, "deck": deck, "indexIds": list(index),
              "airCounts": {i: counts[i] for i in index if i in counts},
              "lastScheduled": {i: last[i] for i in index if i in last},
              "spotlightSlots": sorted(spotlight_slots)[-14:], "index": entries, "source": channel["source"], "indexedAt": old.get("indexedAt", now)}
    if cursor < now + HORIZON and index:
        public["warnings"].append("This source contains very short programs; the preparation limit was reached. The scheduler will continue on its next run.")
    public["version"] = digest(programs)
    public["generatedAt"] = now
    return public


def prepare(client, data_dir, channel_id=None, only_changed=False):
    outcomes = {}
    # Separate lock keeps readers and catalog autosaves responsive during indexing.
    with catalog.catalog_lock(Path(data_dir) / "programming", timeout=1):
        # One resolved channel view: pre-migration this is the catalog; after
        # the library migration it is the owner's authoritative library, so a
        # GUI edit and the scheduler can never disagree.
        from justwatch import channel_service
        if channel_service.library_active(data_dir):
            custom = channel_service.custom_channels_legacy(data_dir)
            current = {"revision": channel_service.directory_revision(data_dir),
                       "channels": custom}
        else:
            current = catalog.load(data_dir)
        for channel in current["channels"]:
            if channel_id and channel_id != channel["id"]:
                continue
            if not channel["enabled"] or policy(channel.get("programming"))["mode"] == "fixed":
                continue
            try:
                prior = read(data_dir, channel["id"])
                signature = digest([channel["source"], channel["sort"], channel["seed"], policy(channel.get("programming"))])
                if only_changed and prior and prior["configuration"] == signature:
                    continue
                now = int(time.time() * 1000)
                reusable = prior and prior.get("index") is not None and prior.get("configuration") == signature and now - prior.get("indexedAt", 0) < 6 * HOUR
                entries = prior["index"] if reusable else index_source(client, channel)
                publication = build(channel, entries, prior)
                publication["indexedAt"] = prior["indexedAt"] if reusable else now
                # A concurrent editor may have changed membership during indexing.
                with catalog.catalog_lock(data_dir):
                    if channel_service.library_active(data_dir):
                        latest_row = channel_service.get_channel(data_dir, channel["id"])
                        latest = channel_service.as_legacy_catalog_channel(latest_row) if latest_row else None
                    else:
                        latest = next((c for c in catalog.load(data_dir)["channels"] if c["id"] == channel["id"]), None)
                    if latest != channel:
                        outcomes[channel["id"]] = "changed_during_build"
                        continue
                    snapshots.write_json(path(data_dir, channel["id"]), publication)
                outcomes[channel["id"]] = "ready"
            except Exception as exc:
                outcomes[channel["id"]] = str(exc)
    return {"channels": outcomes}


def schedule(data_dir, channel_id, at=None, limit=50):
    at = int(time.time() * 1000) if at is None else at
    data = read(data_dir, channel_id)
    if data is None:
        return {"status": "preparing", "programs": []}
    programs = data["programs"]
    result = [p for p in programs if p["endEpochMs"] > at][:max(1, min(50, limit))]
    status = "ready"
    # Deterministic emergency loop: every client sees the same airing boundaries.
    # Continuing publications (schema 3) carry the block explicitly, so the
    # fallback stays identical even after retention prunes the old programs —
    # the engine's recovery replays exactly what this loop served.
    if not result and programs and at >= programs[-1]["endEpochMs"]:
        status = "repeat"
        block = data.get("encoreBlock") or programs[-50:]
        block = [b for b in block if isinstance(b, dict) and
                 isinstance(b.get("startEpochMs"), int) and isinstance(b.get("endEpochMs"), int)]
        length = sum(p["endEpochMs"] - p["startEpochMs"] for p in block)
        if length <= 0:
            return {"status": "preparing", "programs": []}
        # Anchor on the BLOCK's own last end: for schema 1/2 (block is
        # programs[-50:]) that is the historical behavior; for a stored
        # continuing block it keeps the phase identical to the engine's
        # recovery math.
        anchor = block[-1]["endEpochMs"]
        cursor = anchor + ((at - anchor) // length) * length
        while len(result) < min(50, limit):
            for p in block:
                end = cursor + p["endEpochMs"] - p["startEpochMs"]
                if end > at:
                    result.append({**p, "startEpochMs": cursor, "endEpochMs": end,
                                   "airingId": digest([channel_id, cursor, p["item"]["id"]]), "block": "Encore"})
                cursor = end
                if len(result) >= min(50, limit):
                    break
    return {"status": status, "channelId": channel_id, "version": data["version"],
            "programs": result, "preparedThrough": data["preparedThrough"],
            "sourceTotal": data["sourceTotal"], "warnings": data["warnings"],
            "generatedAt": data["generatedAt"], "serverNow": int(time.time() * 1000)}


def desk(data_dir, channels):
    rows = []
    sets = {}
    for channel in channels:
        data = read(data_dir, channel["id"])
        if data:
            sets[channel["id"]] = set(data["indexIds"])
            counts = data["airCounts"]
            rows.append({"id": channel["id"], "name": channel["name"], "sourceTotal": data["sourceTotal"],
                         "scheduledUnique": len(counts), "preparedThrough": data["preparedThrough"],
                         "warnings": data["warnings"], "version": data["version"]})
    for row in rows:
        ids = sets[row["id"]]
        row["overlap"] = [{"id": other["id"], "name": other["name"],
                           "percent": round(100 * len(ids & sets[other["id"]]) / max(1, len(ids)))}
                          for other in rows if other != row and ids & sets[other["id"]]]
    return {"channels": rows}


def preview(data_dir, channel, now=None):
    previous = read(data_dir, channel["id"])
    # source_key keeps publications stored before multi-tag (source without
    # "ids") reusable instead of forcing a pointless re-index.
    if not previous or lineup.source_key(previous.get("source") or {}) != lineup.source_key(channel["source"]) \
            or not previous.get("index"):
        return {"status": "needsIndex", "programs": [], "message": "Prepare this channel once before trying different programming."}
    now = int(time.time() * 1000) if now is None else now
    draft = build(channel, previous["index"], previous, now=now)
    return {"status": "preview", "programs": [p for p in draft["programs"] if p["endEpochMs"] > now][:20],
            "warnings": draft["warnings"], "sourceTotal": draft["sourceTotal"], "effectiveAt": next((p["startEpochMs"] for p in draft["programs"] if p["airingId"] not in {a["airingId"] for a in previous["programs"]}), draft["preparedThrough"])}
