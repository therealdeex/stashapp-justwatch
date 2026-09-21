"""Continuing programming: full-library broadcasts for activated network channels.

Activation is rollout-gated: an operator file in the DATA directory
(``continuing-networks.json``) lists the network ids that run continuing
schedules. Deploying new code alone never activates anything, and networks.json
(the compiled editorial artifact) is never the switch — the two concerns stay
separate exactly like the catalog vs. the compiled tier.

Engine shape (docs/CONTINUING-PROGRAMMING-PLAN.md, phase 2/3):

* One publication file per channel, schema 2, stored beside the custom
  (schema 1) publications. Airings carry wall-clock start/end like always.
* Durable consumption state (pass/deck/counts/last-scheduled/recent/arrivals)
  lives in the file and is advanced ONLY at the COMMITTED boundary — the
  airings that start before ``now + PROTECT`` (whole-airing protection). The
  flexible future beyond that boundary is provisional: building it mutates a
  working copy of the state, so discarding it on a replan can never
  double-count or lose scenes (the trap the old single-shot builder had).
* Retained flexible airings are replayed into the working copy at the start of
  every build, which reproduces exactly the provisional state that placed them.
* New library arrivals (ids that appear in the index without a source change)
  enter a pending queue and a persistent credit accumulator spends ~15% of
  flexible slots on them; a bootstrap credit lets the first arrival air at the
  first arrival-driven replan instead of waiting for credits to accrue.
* Selection is bounded (a scan window over the deterministic deck order), with
  a soft priority tuple: repeat cooldown > same-local-window avoidance >
  performer/studio spacing > exposure fairness > deck order. Every preference
  relaxes rather than blocks on thin libraries.

Timestamps in storage and API are UTC epoch milliseconds; the local viewing
timezone for time-of-day comparisons comes from ``JUSTWATCH_PROGRAMMING_TZ``
(default America/Toronto).
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import math
import os
import re
import time
import zoneinfo
from pathlib import Path

from justwatch import catalog, lineup, networks, programming
from justwatch.programming import digest

HOUR = 3_600_000
#: Rolling publication target (plan default; measured by the simulator).
HORIZON = 168 * HOUR
#: Whole airings starting before now+PROTECT are committed and never replanned.
PROTECT = 24 * HOUR
#: Past airings retained inside the publication file.
RETENTION = 48 * HOUR
#: How long aired intervals feed time-of-day metrics.
AIRED_RETENTION = 30 * 24 * HOUR
#: Two cache lifetimes of client notice before a policy replan moves the future.
NOTICE = 120_000
#: Share of flexible slots offered to new arrivals (plan default).
NEW_SHARE = 0.15
NEW_SHARE_MIN = 0.05
NEW_SHARE_MAX = 0.30
#: Credit accumulator bounds: 1 credit = 1 arrival slot; the cap bounds a bulk
#: import's burst while the share drains the rest over time.
MAX_CREDITS = 4.0
CANDIDATE_SCAN = 256
MAX_PROGRAMS = 20_000
MAX_INDEX = 100_000
INDEX_TTL = 6 * HOUR
#: Per-task bound on (re)indexed channels so one run cannot hammer every source.
INDEX_BUDGET = 40
#: Aired-interval bookkeeping caps (bounded, pruned every publication).
AIRED_PER_SCENE = 20
AIRED_GLOBAL = 200_000
#: Three-hour local viewing window, compared over the last seven days.
TOD_WINDOW_HOURS = 3
TOD_COMPARE = 7 * 24 * HOUR

SCHEMA = 2
MODE = "continuing"

QUERY = """
query ContinuingIndex($filter: FindFilterType!, $scene_filter: SceneFilterType!) {
 findScenes(filter: $filter, scene_filter: $scene_filter) {
  count scenes { id title date created_at studio { id name } performers { id }
   files { duration } paths { preview } }
 }
}
"""

_NETWORK_ID_RE = re.compile(r"^net_[0-9a-f]{8}$")
_ROLLOUT_NAME = "continuing-networks.json"


def now_ms() -> int:
    return int(time.time() * 1000)


def programming_timezone(name: str | None = None) -> zoneinfo.ZoneInfo | _dt.timezone:
    """The IANA zone for local viewing-window comparisons (UTC fallback)."""
    name = name or os.environ.get("JUSTWATCH_PROGRAMMING_TZ") or "America/Toronto"
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        return _dt.timezone.utc


# ---------------------------------------------------------------------------
# Rollout: the operational activation switch (never networks.json)
# ---------------------------------------------------------------------------


def rollout_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _ROLLOUT_NAME


def load_rollout(data_dir: str | Path) -> dict:
    """The activation allowlist: ``{"enabled": bool, "networkIds": [...]}``.

    A missing file is the shipped default (nothing activated). A present but
    malformed file raises — an operator half-editing the rollout must get a
    loud failure in the task, not a silently narrowed activation.
    """
    path = rollout_path(data_dir)
    if not path.exists():
        return {"enabled": False, "networkIds": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"continuing rollout file unreadable at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("continuing rollout file must be a JSON object")
    ids = raw.get("networkIds", [])
    if not isinstance(ids, list) or any(
        not isinstance(i, str) or not _NETWORK_ID_RE.match(i) for i in ids
    ):
        raise ValueError("continuing rollout networkIds must be a list of net_XXXXXXXX ids")
    return {"enabled": bool(raw.get("enabled", True)), "networkIds": ids}


def try_load_rollout(data_dir: str | Path) -> dict:
    """Read-path variant: a broken rollout file reads as fully deactivated.

    Sync reads (Directory/Schedule) must never fail over an ops typo, and they
    cannot disagree with the task about what airs: nothing publishes while
    prepare keeps failing on the broken file.
    """
    try:
        return load_rollout(data_dir)
    except ValueError:
        return {"enabled": False, "networkIds": []}


def authored_mode(channel: dict) -> str:
    """The CSV-authored policy token, "" when the row carries none (legacy)."""
    prog = channel.get("programming")
    if not isinstance(prog, dict):
        return ""
    mode = prog.get("mode")
    return mode if mode in ("fixed", MODE, "explore", "discovery") else ""


def resolved_mode(channel: dict, rollout: dict) -> str:
    """Precedence: an explicit authored ``fixed`` policy PINS a network off
    (editorial veto); otherwise the rollout allowlist decides activation.
    Authored ``continuing`` records intent but never bypasses the rollout —
    broad activation stays a deliberate operator step."""
    if authored_mode(channel) == "fixed":
        return "fixed"
    if rollout.get("enabled") and channel.get("id") in rollout.get("networkIds", []):
        return MODE
    return "fixed"


def active_channels(data_dir: str | Path) -> list[dict]:
    """Network channel rows currently activated for continuing programming."""
    rollout = load_rollout(data_dir)
    if not rollout["enabled"]:
        return []
    return [
        ch for ch in networks.load()["channels"]
        if resolved_mode(ch, rollout) == MODE
    ]


def network_policy(channel: dict) -> dict:
    """The continuing scheduling policy for a network (authored overrides for
    the numeric knobs only; mode/spotlight stay the engine's own)."""
    cfg = {"mode": MODE, "spacing": 1, "repeatHours": 48,
           "spotlight": "none", "spotlightDay": 5, "spotlightHour": 20}
    authored = channel.get("programming")
    if isinstance(authored, dict):
        for key in ("spacing", "repeatHours"):
            value = authored.get(key)
            if type(value) is int:
                cfg[key] = max(0, min(24 if key == "spacing" else 336, value))
    return cfg


def signature_of(channel: dict) -> str:
    return digest([channel["source"], channel["sort"], channel["seed"], network_policy(channel)])


# ---------------------------------------------------------------------------
# Indexing (bounded, fingerprinted, epoch-aware)
# ---------------------------------------------------------------------------


def index_source(client, channel: dict, source_cache: dict | None = None) -> list[dict]:
    """Every eligible playable row of the channel's source, bounded at
    MAX_INDEX. ``source_cache`` dedupes identical (source, epoch) fingerprints
    within one prepare run so overlapping networks share query work."""
    source = channel["source"]
    criteria = q = None
    if source["type"] == "savedFilter":
        criteria, q = lineup.resolve_saved_criteria(client, source["id"])
    scene_filter = lineup.build_scene_filter(source, criteria)
    epoch = lineup.effective_epoch(source)
    cache_key = None
    if source_cache is not None:
        cache_key = (lineup.source_key(source), epoch, q)
        if cache_key in source_cache:
            return [dict(e) for e in source_cache[cache_key]]
    result: dict[str, dict] = {}
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
            item["createdAt"] = row.get("created_at") or ""
            result[item["id"]] = item
        if not rows or page * 250 >= int(node.get("count") or 0):
            break
    else:
        raise ValueError(f"source exceeds {MAX_INDEX:,} rows; narrow this channel before publishing")
    entries = list(result.values())
    if source_cache is not None and cache_key is not None:
        source_cache[cache_key] = [dict(e) for e in entries]
    return entries


def index_fingerprint(entries: list[dict]) -> str:
    """Identity of the index's membership AND durations: any duration edit or
    membership change moves it (both replan the flexible future)."""
    return digest(sorted((e["id"], round(e["duration"], 3)) for e in entries))


def index_jitter(channel_id: str) -> int:
    """Stable per-channel spread (0..INDEX_TTL) so reindex due times never
    align across the tier."""
    return int(digest(["jitter", channel_id]), 16) % INDEX_TTL


# ---------------------------------------------------------------------------
# Selection state: fold/ledger primitives (pure — replay-safe by construction)
# ---------------------------------------------------------------------------


def empty_state(now: int) -> dict:
    return {
        "pass": 0, "deck": [], "airCounts": {}, "lastScheduled": {},
        "recent": [], "arrivalCredits": 1.0,
        "pendingArrivals": [], "arrivalBaseline": now,
    }


def fold_airing(state: dict, airing: dict, channel: dict | None = None,
                index: dict | None = None) -> None:
    """Apply one airing's consumption to a state. Used for committed
    advancement (durable), for replaying retained flexible airings into the
    working copy, and in tests — one function, one semantics.

    When the folded scene is not in the (empty) deck, the airing came from a
    LATER pass: advance the pass and re-deal the full pass deck first (the
    same digest rule the build loop uses), so the durable state's pass/deck
    track exactly what committed — otherwise every rebuild would re-deal
    pass one and the never-aired tail of a large library could never surface."""
    sid = airing["item"]["id"]
    if sid not in state["deck"] and not state["deck"] and channel is not None and index:
        state["pass"] += 1
        state["deck"] = sorted(index, key=lambda i: digest([channel["seed"], state["pass"], i]))
    if sid in state["deck"]:
        state["deck"].remove(sid)
    counts = state["airCounts"]
    counts[sid] = counts.get(sid, 0) + 1
    state["lastScheduled"][sid] = airing["startEpochMs"]
    state["recent"] = (state["recent"] + [airing["item"]])[-10:]
    credits = state["arrivalCredits"] + NEW_SHARE - (1.0 if airing.get("arrival") else 0.0)
    state["arrivalCredits"] = max(0.0, min(MAX_CREDITS, credits))
    if airing.get("arrival"):
        state["pendingArrivals"] = [a for a in state["pendingArrivals"] if a["id"] != sid]


def prune_state(state: dict, index: dict) -> dict:
    """Drop every trace of ids that left the index (deleted/excluded scenes)."""
    keep = lambda ids: [i for i in ids if i in index]  # noqa: E731
    return {
        "pass": state["pass"],
        "deck": keep(state["deck"]),
        "airCounts": {i: c for i, c in state["airCounts"].items() if i in index},
        "lastScheduled": {i: t for i, t in state["lastScheduled"].items() if i in index},
        "recent": [r for r in state["recent"] if r["id"] in index][-10:],
        "arrivalCredits": state["arrivalCredits"],
        "pendingArrivals": [a for a in state["pendingArrivals"] if a["id"] in index],
        "arrivalBaseline": state["arrivalBaseline"],
    }


def prune_aired(aired: dict, now: int) -> dict:
    """Bounded aired-interval history: age, per-scene, and global caps."""
    out = {}
    total = 0
    for sid, intervals in aired.items():
        fresh = [iv for iv in intervals if iv[1] > now - AIRED_RETENTION]
        if fresh:
            kept = fresh[-AIRED_PER_SCENE:]
            out[sid] = kept
            total += len(kept)
    if total > AIRED_GLOBAL:
        pairs = sorted(
            ((iv[0], sid) for sid, ivs in out.items() for iv in ivs),
        )
        drop = {sid for _, sid in pairs[:total - AIRED_GLOBAL]}
        out = {sid: ivs for sid, ivs in out.items() if sid not in drop}
    return out


def record_aired(aired: dict, airing: dict, now: int) -> None:
    """An airing only counts as AIRED once wall clock passed its end —
    scheduled exposure and actual airings stay distinct."""
    if airing["endEpochMs"] > now:
        return
    intervals = aired.setdefault(airing["item"]["id"], [])
    intervals.append([airing["startEpochMs"], airing["endEpochMs"]])
    if len(intervals) > AIRED_PER_SCENE * 2:
        aired[airing["item"]["id"]] = intervals[-AIRED_PER_SCENE:]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _tod_window(at_ms: int, tz) -> int:
    local = _dt.datetime.fromtimestamp(at_ms / 1000, tz)
    return local.hour // TOD_WINDOW_HOURS


def _tod_conflict(aired: dict, last_scheduled: dict, sid: str, at_ms: int, window: int, tz) -> bool:
    for start, _end in aired.get(sid, []):
        if at_ms - start < TOD_COMPARE and _tod_window(start, tz) == window:
            return True
    last = last_scheduled.get(sid)
    if last is not None and at_ms - last < TOD_COMPARE and _tod_window(last, tz) == window:
        return True
    return False


def _pick_next(working: dict, index: dict, aired: dict, cursor: int, tz, cfg: dict):
    """Choose the next scene: an arrival when credits allow, else the best
    candidate in a bounded scan window of the deck (deck order is already the
    deterministic shuffle, so the window is a fair sample). Returns
    ``(scene_id, is_arrival, relaxed)``."""
    pending = [a for a in working["pendingArrivals"] if a["id"] in index]
    working["pendingArrivals"] = pending
    if pending and working["arrivalCredits"] >= 1.0:
        first = min(pending, key=lambda a: (a["firstSeenAt"], a["id"]))
        sid = first["id"]
        working["arrivalCredits"] -= 1.0
        working["pendingArrivals"] = [a for a in pending if a["id"] != sid]
        if sid in working["deck"]:
            working["deck"].remove(sid)
        return sid, True, False
    deck = working["deck"]
    window = deck[:CANDIDATE_SCAN]
    counts = working["airCounts"]
    last = working["lastScheduled"]
    cooldown_ms = cfg["repeatHours"] * HOUR
    window_index = _tod_window(cursor, tz)
    neighbors = working["recent"][-cfg["spacing"]:] if cfg["spacing"] else []

    def key(position_and_id):
        position, sid = position_and_id
        entry = index[sid]
        over_cooldown = 1 if cursor - last.get(sid, -10**18) < cooldown_ms else 0
        tod = 1 if _tod_conflict(aired, last, sid, cursor, window_index, tz) else 0
        spacing = 0
        if neighbors:
            for r in neighbors:
                if (entry["studioId"] and entry["studioId"] == r.get("studioId")) or \
                        set(entry["performerIds"]) & set(r.get("performerIds", [])):
                    spacing = 1
                    break
        # Least-recently-aired tie-breaking (never-aired first: 0 sorts before
        # any real timestamp) — without it, a pass-boundary rebuild could
        # re-pick the scene that just aired at the pass's end.
        recency = last.get(sid, 0)
        return (over_cooldown, tod, spacing, counts.get(sid, 0), recency, position)

    best_position, best = min(enumerate(window), key=key)
    relaxed = key((best_position, best))[:3] != (0, 0, 0)
    return best, False, relaxed


def build(channel: dict, entries: list[dict], previous: dict | None, now: int, tz=None) -> dict:
    """Pure publication builder; deterministic for the same inputs and time.

    ``previous`` is the prior publication dict (schema 2) or None. The durable
    state in the result reflects ONLY committed airings; the flexible future is
    provisional and replayed from that state on every subsequent build.
    """
    tz = tz or programming_timezone()
    now = int(now)
    cfg = network_policy(channel)
    index = {e["id"]: e for e in entries}
    fingerprint = index_fingerprint(entries)
    signature = signature_of(channel)
    epoch = lineup.effective_epoch(source=channel["source"])
    old = previous if isinstance(previous, dict) else {}
    old_programs = [p for p in old.get("programs", [])
                    if isinstance(p, dict) and p.get("endEpochMs", 0) > now - RETENTION]
    warnings: set[str] = set(old.get("warnings", [])) if old.get("configuration") == signature else set()
    degraded = False

    if old.get("configuration") != signature:
        # Policy/source change: restart selection state, re-baseline arrivals,
        # keep whole airings through a short client-notice boundary.
        boundary = next((p["endEpochMs"] for p in old_programs if p["endEpochMs"] >= now + NOTICE), now)
        kept = [p for p in old_programs if p["startEpochMs"] < boundary]
        state = empty_state(now)
        for airing in kept:
            fold_airing(state, airing, channel, index)
        aired = prune_aired(old.get("aired", {}), now)
        programs = kept
        committed = len(kept)
        cursor = max(now, kept[-1]["endEpochMs"] if kept else now)
    else:
        state = copy.deepcopy(old.get("state") or empty_state(now))
        aired = dict(old.get("aired") or {})
        # The committed boundary is identified by AIRING TIME, never by index:
        # the retained list's front is pruned every run (48h retention), so a
        # positional cursor would silently stop advancing as the window slides.
        prev_through = int(old.get("committedThrough", 0))
        cutoff = now + PROTECT
        committed = 0
        for airing in old_programs:
            if airing["startEpochMs"] < cutoff:
                committed += 1
            else:
                break
        old_ids = set(old.get("indexIds", []))
        removed = old_ids - set(index)
        added = set(index) - old_ids
        # Ids that left the source vanish from every state structure NOW —
        # even when no future airing references them (no replan trigger): a
        # deck entry for a deleted scene would crash or re-air it.
        if removed:
            state = prune_state(state, index)
        # Replan triggers, computed BEFORE folding so a cut inside the newly
        # committed region never folds airings that are about to be canceled.
        replan_from = None
        if removed:
            affected = next((i for i, p in enumerate(old_programs)
                             if p["endEpochMs"] > now and p["item"]["id"] not in index), None)
            if affected is not None:
                replan_from = affected
        if replan_from is None and old.get("indexFingerprint") not in ("", None) \
                and old.get("indexFingerprint") != fingerprint and old_programs:
            # A changed fingerprint alone is NOT a replan: additions are the
            # arrival machinery's job. Only common ids whose DURATION moved
            # invalidate published flexible lengths.
            prior_index = {e["id"]: e for e in (old.get("index") or [])}
            if any(i in prior_index and prior_index[i].get("duration") != index[i]["duration"]
                   for i in index):
                replan_from = committed
        if replan_from is not None:
            # Release the canceled future's reservations: fold (and aired-record)
            # only up to the cut, give back the cut airings' exposure counts, and
            # prune the removed ids from every state structure. Give-back covers
            # only the durably folded committed region — flexible airings were
            # never counted, so "giving them back" would drain real history.
            # Cut scenes stay out of this pass's deck (their fold may already be
            # durable); the next pass refill resurfaces them, so nothing is lost.
            keep_upto = min(committed, replan_from)
            for airing in old_programs[:keep_upto]:
                if prev_through < airing["startEpochMs"] < cutoff:
                    fold_airing(state, airing, channel, index)
                    record_aired(aired, airing, now)
            for airing in old_programs[keep_upto:committed]:
                sid = airing["item"]["id"]
                if state["airCounts"].get(sid, 0) > 0:
                    state["airCounts"][sid] -= 1
            committed = keep_upto
            programs = old_programs[:keep_upto]
            state = prune_state(state, index)
            cursor = max(now, programs[-1]["endEpochMs"] if programs else now)
        else:
            for airing in old_programs[:committed]:
                if prev_through < airing["startEpochMs"] < cutoff:
                    fold_airing(state, airing, channel, index)
                    record_aired(aired, airing, now)
            programs = list(old_programs)
            cursor = max(now, programs[-1]["endEpochMs"] if programs else now)
        if added:
            for sid in sorted(added, key=lambda i: digest([channel["seed"], i])):
                state["deck"].append(sid)
                state["pendingArrivals"].append({"id": sid, "firstSeenAt": now})

    # Outage recovery: if the schedule expired, finish the deterministic encore
    # airing currently on air before fresh programming resumes (shared fallback
    # math with the custom engine — every client sees the same boundaries).
    if programs and programs[-1]["endEpochMs"] <= now:
        block = [p for p in old.get("programs", []) if isinstance(p, dict)][-50:] or programs[-50:]
        length = sum(p["endEpochMs"] - p["startEpochMs"] for p in block)
        if length > 0:
            offset = block[-1]["endEpochMs"] + ((now - block[-1]["endEpochMs"]) // length) * length
            for airing in block:
                end = offset + airing["endEpochMs"] - airing["startEpochMs"]
                if end > now:
                    encore = {**airing, "startEpochMs": offset, "endEpochMs": end,
                              "airingId": digest([channel["id"], offset, airing["item"]["id"]]),
                              "block": "Encore"}
                    programs.append(encore)
                    fold_airing(state, encore, channel, index)
                    record_aired(aired, encore, now)
                    committed = len(programs)
                    degraded = True
                    break
                offset = end
        cursor = max(cursor, programs[-1]["endEpochMs"])

    # Arrival-driven replan: when unplaced arrivals are waiting and credits are
    # due, rebuild the flexible future so they land inside the 24–72h target.
    # An arrival already placed in the retained flexible zone is left alone —
    # its first airing must not slide an hour every run waiting to commit.
    # The committed prefix is untouched either way.
    if len(programs) > committed and state["arrivalCredits"] >= 1.0:
        placed = {a["item"]["id"] for a in programs[committed:] if a.get("arrival")}
        unplaced = [a for a in state["pendingArrivals"] if a["id"] not in placed]
        if unplaced:
            programs = programs[:committed]
            cursor = max(now, programs[-1]["endEpochMs"] if programs else now)

    working = copy.deepcopy(state)
    for airing in programs[committed:]:
        fold_airing(working, airing, channel, index)  # provisional replay

    relaxed_any = False
    while index and cursor < now + HORIZON and len(programs) < MAX_PROGRAMS:
        if not working["deck"]:
            working["pass"] += 1
            working["deck"] = sorted(index, key=lambda i: digest([channel["seed"], working["pass"], i]))
        sid, is_arrival, relaxed = _pick_next(working, index, aired, cursor, tz, cfg)
        relaxed_any = relaxed_any or relaxed
        item = index[sid]
        end = cursor + max(1000, round(item["duration"] * 1000))
        airing = {"airingId": digest([channel["id"], cursor, sid]), "startEpochMs": cursor,
                  "endEpochMs": end, "item": item, "block": ""}
        if is_arrival:
            airing["arrival"] = True
        programs.append(airing)
        fold_airing(working, airing, channel, index)
        cursor = end
    if relaxed_any:
        warnings.add("Some repeat, time-of-day, or spacing preferences could not be met by this lineup.")
    if cursor < now + HORIZON and index and len(programs) >= MAX_PROGRAMS:
        warnings.append("This source contains very short programs; the preparation limit was reached. "
                        "The scheduler will continue on its next run.")
    pending_ages = [now - a["firstSeenAt"] for a in state["pendingArrivals"]]
    if pending_ages and max(pending_ages) > 72 * HOUR:
        # Bulk imports drain at the configured share; report honestly instead
        # of promising a deadline the capacity cannot meet.
        warnings.add(f"{len(pending_ages)} new additions are waiting; at a "
                     f"{round(NEW_SHARE * 100)}% share the oldest has waited "
                     f"{max(pending_ages) // HOUR}h and the rest drain gradually.")

    aired = prune_aired(aired, now)
    # The durable commit cursor: the start time of the last committed airing
    # (monotone — next run folds exactly the airings beyond it).
    committed_through = max(
        [prev_through if old.get("configuration") == signature else 0]
        + [a["startEpochMs"] for a in programs[:committed]]
    ) if committed else max(prev_through if old.get("configuration") == signature else 0, 0)
    publication = {
        "schema": SCHEMA,
        "channelId": channel["id"],
        "mode": MODE,
        "configuration": signature,
        "programs": programs,
        "committedCount": committed,
        "committedThrough": committed_through,
        "preparedThrough": cursor,
        "sourceTotal": len(index),
        "warnings": sorted(warnings),
        "version": digest(programs),
        "generatedAt": now,
        "state": prune_state(state, index),
        "aired": aired,
        "index": entries,
        "indexIds": list(index),
        "indexFingerprint": fingerprint,
        "indexEpoch": epoch,
        "source": channel["source"],
        "degraded": degraded,
    }
    return publication


def read(data_dir: str | Path, channel_id: str) -> dict | None:
    """A continuing publication (schema 2), or None when never published.

    A schema-1 file at a network path (a custom-engine artifact that can only
    exist on a hand-migrated dev box) reads through ``programming.read``; the
    next prepare replaces it with schema 2 state.
    """
    path = programming.path(data_dir, channel_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get("schema") == SCHEMA:
        if not isinstance(data.get("programs"), list) or not isinstance(data.get("state"), dict):
            raise ValueError("unreadable published schedule")
        return data
    return programming.read(data_dir, channel_id)


# ---------------------------------------------------------------------------
# Preparation (task-only): staggered indexing, fair budget, atomic publish
# ---------------------------------------------------------------------------


def _scheduler_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "programming" / "scheduler.json"


def _load_scheduler(data_dir: str | Path) -> dict:
    try:
        return json.loads(_scheduler_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_scheduler(data_dir: str | Path, state: dict) -> None:
    programming.snapshots.write_json(_scheduler_path(data_dir), state)


def _index_due(prior: dict | None, signature: str, epoch: str, now: int, channel: dict) -> bool:
    if prior is None or prior.get("configuration") != signature:
        return True
    if prior.get("schema") != SCHEMA or not prior.get("index"):
        return True
    if prior.get("indexEpoch", "") != epoch:
        return True  # dynamic-source membership moved with its cutoff day
    ttl = INDEX_TTL + index_jitter(channel["id"])
    return now - int(prior.get("indexedAt", 0)) >= ttl


def prepare(client, data_dir, channel_id: str | None = None, now: int | None = None,
            budget: int = INDEX_BUDGET) -> dict:
    """Advance every activated network's publication. Per-channel failure
    isolation; one shared source-dedup cache; a persisted fair cursor so a
    bounded batch cannot always service the same first channels."""
    data_dir = Path(data_dir)
    now = now_ms() if now is None else now
    channels = active_channels(data_dir)
    if channel_id:
        channels = [ch for ch in channels if ch["id"] == channel_id]
    outcomes: dict[str, str] = {}
    if not channels:
        return {"channels": outcomes}

    # Coverage-first ordering: whatever expires soonest is prepared first.
    def coverage(ch: dict) -> int:
        prior = read(data_dir, ch["id"])
        if prior is None or prior.get("configuration") != signature_of(ch):
            return -1
        return int(prior.get("preparedThrough", 0)) - now

    ordered = sorted(channels, key=coverage)
    cursor_pos = int(_load_scheduler(data_dir).get("cursor", 0)) % len(ordered)
    ordered = ordered[cursor_pos:] + ordered[:cursor_pos]

    source_cache: dict = {}
    indexed = 0
    for channel in ordered:
        try:
            signature = signature_of(channel)
            prior = read(data_dir, channel["id"])
            due = _index_due(prior, signature, lineup.effective_epoch(channel["source"]), now, channel)
            if due and indexed >= budget:
                outcomes[channel["id"]] = "index_budget_exhausted"
                continue
            if due:
                entries = index_source(client, channel, source_cache)
                indexed += 1
                indexed_at = now
            else:
                entries = prior["index"]
                indexed_at = int(prior.get("indexedAt", now))
            publication = build(channel, entries, prior if prior and prior.get("schema") == SCHEMA else None, now)
            publication["indexedAt"] = indexed_at
            # Commit guard: membership or a sibling writer may have moved under
            # us during indexing; never overwrite a newer generation.
            with catalog_lock_for(data_dir):
                latest = networks.get(channel["id"])
                if latest is None or signature_of(latest) != signature:
                    outcomes[channel["id"]] = "changed_during_build"
                    continue
                current = read(data_dir, channel["id"])
                if current is not None and current.get("schema") == SCHEMA \
                        and (prior is None or current.get("version") != prior.get("version")):
                    outcomes[channel["id"]] = "changed_during_build"
                    continue
                programming.snapshots.write_json(programming.path(data_dir, channel["id"]), publication)
            outcomes[channel["id"]] = "ready" if not publication["degraded"] else "recovered_from_outage"
        except Exception as exc:  # one channel must never block the tier
            outcomes[channel["id"]] = f"{type(exc).__name__}: {exc}"
    _write_scheduler(data_dir, {"cursor": cursor_pos + len(outcomes)})
    return {"channels": outcomes}


def catalog_lock_for(data_dir):
    return catalog.catalog_lock(data_dir, timeout=30.0)


# ---------------------------------------------------------------------------
# Status manifest: the lightweight surface Directory/Desk/ops read
# ---------------------------------------------------------------------------


def status_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "programming" / "status.json"


def read_status(data_dir: str | Path) -> dict:
    try:
        data = json.loads(status_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def channel_status(publication: dict | None, now: int, mode: str) -> dict:
    if not publication:
        return {"mode": mode, "ready": False}
    counts = publication.get("airCounts") or publication.get("state", {}).get("airCounts") or {}
    prepared_through = int(publication.get("preparedThrough", 0))
    coverage_hours = max(0, round((prepared_through - now) / HOUR, 1))
    return {
        "mode": mode,
        "ready": True,
        "version": publication.get("version"),
        "generatedAt": publication.get("generatedAt"),
        "preparedThrough": prepared_through,
        "coverageHours": coverage_hours,
        "sourceTotal": publication.get("sourceTotal"),
        "scheduledUnique": len(counts),
        "degraded": bool(publication.get("degraded")),
        "warnings": publication.get("warnings", []),
        # Alert threshold: a healthy hourly scheduler never lets ready coverage
        # drop below 24h; anything below is one missed run from encore.
        "expiring": coverage_hours < 24,
    }


def write_status(data_dir: str | Path, run: dict, now: int | None = None) -> dict:
    """Atomically publish the lightweight status manifest: per-channel schedule
    status (custom + network) plus the last run's outcomes, so Directory and
    ops tooling never parse large schedule files."""
    data_dir = Path(data_dir)
    now = now_ms() if now is None else now
    channels: dict[str, dict] = dict(read_status(data_dir).get("channels") or {})
    try:
        for channel in catalog.load(data_dir).get("channels", []):
            mode = programming.policy(channel.get("programming"))["mode"]
            if mode == "fixed":
                channels.pop(channel["id"], None)
                continue
            prior = programming.read(data_dir, channel["id"])
            channels[channel["id"]] = channel_status(prior, now, mode)
    except catalog.CatalogError:
        pass  # a corrupt catalog must not take the status manifest down
    for channel in active_channels_safe(data_dir):
        prior = read(data_dir, channel["id"])
        channels[channel["id"]] = channel_status(prior, now, MODE)
    manifest = {
        "schema": 1,
        "generatedAt": now,
        "lastRun": run,
        "channels": channels,
    }
    programming.snapshots.write_json(status_path(data_dir), manifest)
    return manifest


def active_channels_safe(data_dir: str | Path) -> list[dict]:
    try:
        return active_channels(data_dir)
    except (ValueError, networks.NetworksError):
        return []


def desk(data_dir: str | Path, offset: int = 0, limit: int = 50) -> dict:
    """Bounded network programming diagnostics (NO pairwise overlap — that is
    offline/on-demand work, never a synchronous all-pairs computation)."""
    status = read_status(data_dir)
    rollout = try_load_rollout(data_dir)
    rows = [
        {**entry, "id": cid}
        for cid, entry in sorted(status.get("channels", {}).items())
        if entry.get("mode") == MODE
    ]
    total = len(rows)
    return {
        "rollout": {"enabled": rollout["enabled"], "active": total},
        "total": total,
        "offset": offset,
        "channels": rows[offset:offset + max(1, min(200, limit))],
    }
