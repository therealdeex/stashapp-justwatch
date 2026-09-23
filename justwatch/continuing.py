"""Continuing programming: full-library broadcasts for activated network channels.

Activation is rollout-gated: an operator file in the DATA directory
(``continuing-networks.json``) lists the network ids that run continuing
schedules and a stage (``prepare`` builds publications without advertising
them; ``active`` serves them). Deploying new code alone never activates
anything, and networks.json (the compiled editorial artifact) is never the
switch — the two concerns stay separate exactly like the catalog vs. the
compiled tier.

Engine shape (docs/CONTINUING-PROGRAMMING-PLAN.md phase 2/3, remediated per
docs/CONTINUING-PROGRAMMING-REMEDIATION-PLAN.md):

* One publication file per channel, schema 3, stored beside the custom
  (schema 1) publications. Airings carry wall-clock start/end like always.
* ONE consumption transition (``apply_airing``) advances pass/deck/counts/
  recency/credits/arrivals. Live scheduling and replay both call it exactly
  once per airing, so a replayed timeline always lands on the state the live
  loop built — the double-spend/replay-mismatch trap the schema-2 engine had.
* The DURABLE ledger is the ``checkpoint``: consumption state at the
  actually-aired boundary (``airedThrough``). It advances only when wall clock
  passes an airing's end, via an independent actual-airing cursor — scheduled
  exposure and actual airings stay distinct, and time-of-day preferences see
  real multi-airing history.
* The durable ``state`` an airing sees is always RE-DERIVED per build:
  checkpoint + replay of the committed reservations (airings inside the
  now+PROTECT whole-airing protection that have not completed). Canceled
  reservations are therefore released by construction — a cut scene simply
  never entered the checkpoint, so it is still in the pass deck.
* Policy edits (spacing/repeatHours/newShare) keep the 24-hour protected
  prefix and the whole durable ledger; only the flexible future beyond it is
  rebuilt. Lifetime consumption is never reset by a preference change.
* New library arrivals (ids that appear in the index without a source change,
  whose createdAt is not older than the index baseline) enter a pending queue
  and a persistent credit accumulator spends ~15% of flexible slots on them.
  ANY first exposure satisfies a pending arrival — a scene picked normally is
  never re-picked as an arrival.
* Selection (``pick_next``) is PURE and bounded (a scan window over the
  deterministic deck order), with a soft priority tuple: repeat cooldown >
  same-local-window avoidance > performer/studio spacing > exposure fairness
  > coarse recency bucket > deck order. Recency is bucketed so the per-pass
  shuffle stays meaningful (a homogeneous library does not replay the same
  permutation every pass), and every preference relaxes rather than blocks
  on thin libraries.
* Publications carry a ``generation`` token over the whole durable state
  (programs + checkpoint + cursors + signatures), used as the commit guard:
  a stale write with an identical program list but different state is
  rejected, and rollout activation is revalidated inside the commit lock.

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
import shutil
import time
import zoneinfo
from pathlib import Path

from justwatch import catalog, criteria, lineup, networks, programming
from justwatch.library import LibraryError
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
#: Share of flexible slots offered to new arrivals (plan default; authored
#: ``newShare`` policy overrides within the bounded range).
NEW_SHARE = 0.15
NEW_SHARE_MIN = 0.05
NEW_SHARE_MAX = 0.30
#: Credit accumulator bounds: 1 credit = 1 arrival slot; the cap bounds a bulk
#: import's burst while the share drains the rest over time.
MAX_CREDITS = 4.0
CANDIDATE_SCAN = 256
#: Recency bucketing (R3): stale candidates rank by 12h buckets capped at 7
#: days, so exact-minute recency never dominates the per-pass deck shuffle
#: (a homogeneous library must not replay one permutation). Airings within
#: REPEAT_GUARD are instead hard-avoided by exact recency: no immediate
#: repeat when alternatives exist, without a permanent global order.
RECENCY_BUCKET = 12 * HOUR
RECENCY_BUCKETS = 14
REPEAT_GUARD = 6 * HOUR
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
#: Consecutive per-channel failures before preparation backs off a source.
FAILURE_BACKOFF_LIMIT = 3
FAILURE_BACKOFF = 2 * HOUR

SCHEMA = 3
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


def _utc_date(ms: int) -> _dt.date:
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# Rollout: the operational activation switch (never networks.json)
# ---------------------------------------------------------------------------


def rollout_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _ROLLOUT_NAME


def load_rollout(data_dir: str | Path) -> dict:
    """The activation allowlist: ``{"enabled": bool, "networkIds": [...],
    "stage": "prepare"|"active"}``.

    A missing file is the shipped default (nothing activated). A present but
    malformed file raises — an operator half-editing the rollout must get a
    loud failure in the task, not a silently narrowed activation. ``enabled``
    must be a real boolean (the string "false" is a loud error, not true),
    and ``stage`` distinguishes preparing publications from advertising them.
    """
    path = rollout_path(data_dir)
    if not path.exists():
        return {"enabled": False, "networkIds": [], "stage": "active"}
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
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("continuing rollout 'enabled' must be a boolean")
    stage = raw.get("stage", "active")
    if stage not in ("prepare", "active"):
        raise ValueError("continuing rollout 'stage' must be \"prepare\" or \"active\"")
    return {"enabled": enabled, "networkIds": ids, "stage": stage}


def try_load_rollout(data_dir: str | Path) -> dict:
    """Read-path variant: a broken rollout file reads as fully deactivated.

    Sync reads (Directory/Schedule) must never fail over an ops typo, and they
    cannot disagree with the task about what airs: nothing publishes while
    prepare keeps failing on the broken file.
    """
    try:
        return load_rollout(data_dir)
    except ValueError:
        return {"enabled": False, "networkIds": [], "stage": "active"}


def authored_mode(channel: dict) -> str:
    """The CSV-authored policy token, "" when the row carries none (legacy)."""
    prog = channel.get("programming")
    if not isinstance(prog, dict):
        return ""
    mode = prog.get("mode")
    return mode if mode in ("fixed", MODE, "explore", "discovery") else ""


def scheduled_mode(channel: dict, rollout: dict) -> str:
    """Whether the channel is SCHEDULED for continuing programming under the
    rollout (either stage). An explicit authored ``fixed`` policy PINS a
    network off (editorial veto); authored ``continuing`` never bypasses the
    rollout — broad activation stays a deliberate operator step."""
    if authored_mode(channel) == "fixed":
        return "fixed"
    if rollout.get("enabled") and channel.get("id") in rollout.get("networkIds", []):
        return MODE
    return "fixed"


def resolved_mode(channel: dict, rollout: dict) -> str:
    """The ADVERTISED mode: a staged (``prepare``) rollout builds publications
    without serving them — compatible TVs stay on fixed playback until the
    operator flips the stage at a whole-airing boundary, so activation can
    never put a TV on a schedule that was never published."""
    if scheduled_mode(channel, rollout) != MODE:
        return "fixed"
    return MODE if rollout.get("stage", "active") == "active" else "fixed"


def network_rows(data_dir: str | Path) -> list[dict]:
    """The network tier's rows in the compiled shape. After the library
    migration the authoritative store is the channel library; pre-migration
    deployments keep reading the compiled artifact."""
    from justwatch import channel_service
    if channel_service.library_active(data_dir):
        return channel_service.network_channels_legacy(data_dir)
    return networks.load()["channels"]


def scheduled_channels(data_dir: str | Path) -> list[dict]:
    """Network rows with publications prepared under the rollout (both stages)."""
    rollout = load_rollout(data_dir)
    if not rollout["enabled"]:
        return []
    return [
        ch for ch in network_rows(data_dir)
        if scheduled_mode(ch, rollout) == MODE
    ]


def active_channels(data_dir: str | Path) -> list[dict]:
    """Network channel rows currently activated for continuing programming.

    Preparation runs against the scheduled set (both stages); advertising is
    gated separately by :func:`resolved_mode`."""
    return scheduled_channels(data_dir)


def network_policy(channel: dict) -> dict:
    """The continuing scheduling policy for a network (authored overrides for
    the numeric knobs only; mode/spotlight stay the engine's own). ``newShare``
    is the bounded configurable arrival share from the plan (default 15%)."""
    cfg = {"mode": MODE, "spacing": 1, "repeatHours": 48,
           "spotlight": "none", "spotlightDay": 5, "spotlightHour": 20,
           "newShare": NEW_SHARE}
    authored = channel.get("programming")
    if isinstance(authored, dict):
        for key in ("spacing", "repeatHours"):
            value = authored.get(key)
            if type(value) is int:
                cfg[key] = max(0, min(24 if key == "spacing" else 336, value))
        share = authored.get("newShare")
        if isinstance(share, (int, float)) and not isinstance(share, bool):
            cfg["newShare"] = max(NEW_SHARE_MIN, min(NEW_SHARE_MAX, float(share)))
    return cfg


def source_signature_of(channel: dict) -> str:
    """Identity of WHERE content comes from and HOW the deck is dealt: source,
    sort, seed. A change re-deals the deck (the flexible future rebuilds);
    membership deltas ride the deletion/arrival machinery instead."""
    return digest([channel["source"], channel["sort"], channel["seed"]])


def policy_signature_of(channel: dict) -> str:
    """Identity of the soft PREFERENCES only. A change rebuilds the flexible
    future but never touches the protected prefix or the durable ledger."""
    return digest(network_policy(channel))


def signature_of(channel: dict) -> str:
    return digest([source_signature_of(channel), policy_signature_of(channel)])


# ---------------------------------------------------------------------------
# Indexing (bounded, fingerprinted, epoch-aware)
# ---------------------------------------------------------------------------


def index_source(client, channel: dict, source_cache: dict | None = None) -> list[dict]:
    """Every eligible playable row of the channel's source, bounded at
    MAX_INDEX. ``source_cache`` dedupes identical (source, epoch) fingerprints
    within one prepare run so overlapping networks share query work."""
    source = channel["source"]
    object_filter, q = None, None
    if source["type"] == "savedFilter":
        object_filter, q = lineup.resolve_saved_criteria(client, source["id"])
    if q is None:
        q = criteria.text_query_of(source)
    scene_filter = lineup.build_scene_filter(source, object_filter)
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
# Selection state: the one exactly-once transition (pure pick + single fold)
# ---------------------------------------------------------------------------


def empty_state(now: int) -> dict:
    return {
        "pass": 0, "deck": [], "airCounts": {}, "lastScheduled": {},
        "recent": [], "arrivalCredits": 1.0,
        "pendingArrivals": [], "arrivalBaseline": now,
    }


def _refill_pass(state: dict, channel: dict, index: dict) -> None:
    """Deal the next pass deck (deterministic: seed + pass number + index)."""
    state["pass"] += 1
    state["deck"] = sorted(index, key=lambda i: digest([channel["seed"], state["pass"], i]))


def apply_airing(state: dict, airing: dict, channel: dict | None = None,
                 index: dict | None = None, cfg: dict | None = None) -> None:
    """THE single consumption transition (R1/R2 remediation).

    Construction and replay both advance a state by calling this EXACTLY ONCE
    per airing — live scheduling, reservation replay after a checkpoint
    restore, and schema-2 timeline migration included — so a replayed timeline
    always reproduces the state the live loop built.

    Semantics:
    * an empty deck deals the next pass (identical digest rule everywhere);
    * the scene leaves the remaining deck (once — no double membership);
    * counts/lastScheduled/recent record the SCHEDULED exposure;
    * ANY exposure satisfies a pending arrival (a scene picked normally is
      never re-pickable as an arrival — the out-of-deck re-pick trap);
    * only an arrival-flagged slot spends a credit; every airing accrues the
      configurable share.
    """
    cfg = cfg or {}
    share = float(cfg.get("newShare", NEW_SHARE))
    sid = airing["item"]["id"]
    if not state["deck"] and channel is not None and index:
        _refill_pass(state, channel, index)
    if sid in state["deck"]:
        state["deck"].remove(sid)
    state["airCounts"][sid] = state["airCounts"].get(sid, 0) + 1
    state["lastScheduled"][sid] = airing["startEpochMs"]
    state["recent"] = (state["recent"] + [airing["item"]])[-10:]
    if state["pendingArrivals"]:
        state["pendingArrivals"] = [a for a in state["pendingArrivals"] if a["id"] != sid]
    credits = state["arrivalCredits"] + share - (1.0 if airing.get("arrival") else 0.0)
    state["arrivalCredits"] = max(0.0, min(MAX_CREDITS, credits))


def validate_checkpoint(raw) -> dict:
    """Strict structural validation of the durable ledger (R12): a schema-3
    publication with a missing or corrupt checkpoint RAISES — it is never
    silently defaulted to a fresh empty pass (which would re-air and
    re-count content). Returns a detached deep copy safe to mutate."""
    def fail(why: str) -> None:
        raise ValueError(f"corrupt continuing state: {why}")

    if not isinstance(raw, dict):
        fail("checkpoint missing or not an object")
    allowed = {"pass", "deck", "airCounts", "lastScheduled", "recent",
               "arrivalCredits", "pendingArrivals", "arrivalBaseline"}
    missing = allowed - set(raw)
    if missing:
        fail(f"missing fields {sorted(missing)}")
    if type(raw["pass"]) is not int or raw["pass"] < 0:
        fail("pass")
    if not isinstance(raw["deck"], list) or any(not isinstance(i, str) for i in raw["deck"]):
        fail("deck")
    if not isinstance(raw["airCounts"], dict) or any(
        not isinstance(k, str) or type(v) is not int or v < 0
        for k, v in raw["airCounts"].items()
    ):
        fail("airCounts")
    if not isinstance(raw["lastScheduled"], dict) or any(
        not isinstance(k, str) or type(v) is not int
        for k, v in raw["lastScheduled"].items()
    ):
        fail("lastScheduled")
    if not isinstance(raw["recent"], list) or any(
        not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in raw["recent"]
    ):
        fail("recent")
    credits = raw["arrivalCredits"]
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) \
            or not (0.0 <= float(credits) <= MAX_CREDITS):
        fail("arrivalCredits")
    if not isinstance(raw["pendingArrivals"], list) or any(
        not isinstance(a, dict) or not isinstance(a.get("id"), str)
        or type(a.get("firstSeenAt")) is not int
        for a in raw["pendingArrivals"]
    ):
        fail("pendingArrivals")
    if type(raw["arrivalBaseline"]) is not int:
        fail("arrivalBaseline")
    return copy.deepcopy({key: raw[key] for key in allowed})


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


def _created_ms(value: str) -> int | None:
    """Parse a Stash created_at timestamp; None when absent/unparseable."""
    if not value:
        return None
    try:
        return int(_dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


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


def pick_next(working: dict, index: dict, aired: dict, cursor: int, tz, cfg: dict):
    """PURE candidate choice (R2 remediation: no state mutation — the caller
    applies the chosen airing through :func:`apply_airing` exactly once).

    An arrival is only ever chosen from the REMAINING deck, so a scene picked
    normally (whose first exposure already cleared its pending entry) can
    never be re-picked as an arrival, and an arrival can never come from
    outside the current pass.

    Among deck candidates the soft priority tuple decides: repeat cooldown >
    just-aired guard > same-local-window avoidance > performer/studio spacing
    > exposure fairness > freshness > deck position. Freshness (R3
    remediation): never-aired first; then stale candidates ranked by COARSE
    recency buckets (12h, capped at 7 days) so exact-minute recency never
    recreates the whole previous pass order — the per-pass shuffle stays
    meaningful among stale candidates and a homogeneous library does not
    replay one permutation; anything aired within REPEAT_GUARD ranks after
    every stale candidate by exact recency, so the just-aired scene is truly
    last without imposing a permanent order on the rest. On a thin source the
    tiers collapse and every preference relaxes rather than blocks.
    Returns ``(scene_id, is_arrival, relaxed)``.
    """
    pending = [a for a in working["pendingArrivals"]
               if a["id"] in index and a["id"] in working["deck"]]
    if pending and working["arrivalCredits"] >= 1.0:
        first = min(pending, key=lambda a: (a["firstSeenAt"], a["id"]))
        return first["id"], True, False
    deck = working["deck"]
    window = deck[:CANDIDATE_SCAN]
    counts = working["airCounts"]
    last = working["lastScheduled"]
    cooldown_ms = cfg["repeatHours"] * HOUR
    window_index = _tod_window(cursor, tz)
    neighbors = working["recent"][-cfg["spacing"]:] if cfg["spacing"] else []
    most_recent = max(last.values(), default=None)

    def key(position_and_id):
        position, sid = position_and_id
        entry = index[sid]
        over_cooldown = 1 if cursor - last.get(sid, -10**18) < cooldown_ms else 0
        rec = last.get(sid)
        if rec is None:
            just_aired = 0
            freshness = (0, 0)  # never aired: first exposure wins
        else:
            age = cursor - rec
            just_aired = 1 if age < REPEAT_GUARD else 0
            if just_aired:
                # most recent LAST (S2): within the guard, the one video that
                # aired MOST recently ranks after every other guard candidate,
                # so the scene that just ended never replays while an
                # alternative exists. The rest of the guard tier follows the
                # CURRENT pass's deck order — not last pass's order and not a
                # strict-LRU ranking (uniform-length libraries would otherwise
                # replay one permutation forever or run it backwards).
                freshness = (2, 1) if rec == most_recent else (2, 0)
            else:
                # stale: coarse buckets, staler first — bucketed so that the
                # within-bucket order is the per-pass shuffle, not last pass.
                freshness = (1, RECENCY_BUCKETS - min(age // RECENCY_BUCKET, RECENCY_BUCKETS))
        tod = 0 if just_aired else (1 if _tod_conflict(aired, last, sid, cursor, window_index, tz) else 0)
        spacing = 0
        if neighbors and not just_aired:
            for r in neighbors:
                if (entry["studioId"] and entry["studioId"] == r.get("studioId")) or \
                        set(entry["performerIds"]) & set(r.get("performerIds", [])):
                    spacing = 1
                    break
        return (over_cooldown, just_aired, tod, spacing, counts.get(sid, 0), freshness, position)

    best_position, best = min(enumerate(window), key=key)
    relaxed = key((best_position, best))[:3] != (0, 0, 0)
    return best, False, relaxed


def build(channel: dict, entries: list[dict], previous: dict | None, now: int, tz=None) -> dict:
    """Pure publication builder; deterministic for the same inputs and time.

    ``previous`` is the prior publication dict (schema 3, or schema 2 for the
    one-shot migration path) or None. The durable ledger in the result is the
    CHECKPOINT (actual-aired boundary); the committed future is re-derived
    from it every build, and the flexible future beyond the protection window
    is provisional.
    """
    tz = tz or programming_timezone()
    now = int(now)
    cfg = network_policy(channel)
    index = {e["id"]: e for e in entries}
    fingerprint = index_fingerprint(entries)
    source_sig = source_signature_of(channel)
    policy_sig = policy_signature_of(channel)
    epoch = lineup.effective_epoch(channel["source"], today=_utc_date(now))
    old = previous if isinstance(previous, dict) else {}
    old_programs = [p for p in old.get("programs", [])
                    if isinstance(p, dict) and isinstance(p.get("item"), dict)
                    and isinstance(p.get("item", {}).get("id"), str)
                    and isinstance(p.get("startEpochMs"), int)
                    and isinstance(p.get("endEpochMs"), int)]
    warnings: set[str] = set()
    degraded = False

    # --- durable ledger: strict on schema 3, timeline-rebuilt on migration ---
    if old.get("schema") == SCHEMA:
        checkpoint = validate_checkpoint(old.get("checkpoint"))
    else:
        # Schema 2 (or none): its counters are unreliable by review finding —
        # reconstruct the ledger from the client-visible timeline instead of
        # trusting stored counts. Completed airings fold exactly once below.
        checkpoint = empty_state(now)
    # Deep-copy the aired intervals: nested lists must never alias the input
    # publication (a shallow dict copy let record_aired mutate prior history).
    aired: dict = {}
    for sid, intervals in (old.get("aired") or {}).items():
        if isinstance(intervals, list):
            aired[sid] = [list(iv) for iv in intervals if isinstance(iv, (list, tuple)) and len(iv) == 2]
    aired_through = int(old.get("airedThrough", 0) or 0)

    # --- actual-history advance (R4): independent, idempotent wall-clock ---
    # Every airing that completed since the cursor last advanced folds into
    # the checkpoint EXACTLY ONCE, identified by its end time — never only
    # when future slots first become protected.
    for airing in old_programs:
        if airing["endEpochMs"] > now or airing["endEpochMs"] <= aired_through:
            continue
        apply_airing(checkpoint, airing, channel, index, cfg)
        record_aired(aired, airing, now)
        aired_through = airing["endEpochMs"]

    # --- encore source: captured BEFORE retention pruning (R10). A long
    # outage outruns the 48h retained window; recovery must loop the stored
    # block, which mirrors exactly what the read surface (programming.schedule)
    # served while the scheduler was dark. ---
    stored_block = old.get("encoreBlock")
    encore_block = [a for a in (stored_block if isinstance(stored_block, list)
                                else old_programs[-50:])
                    if isinstance(a, dict) and isinstance(a.get("item"), dict)]
    programs = [p for p in old_programs if p["endEpochMs"] > now - RETENTION]

    # --- committed boundary: whole airings through now+PROTECT ---
    cutoff = now + PROTECT
    committed = 0
    for airing in programs:
        if airing["startEpochMs"] < cutoff:
            committed += 1
        else:
            break

    # --- eligibility reconciliation (R1): cut at the EARLIEST invalid airing
    # only. Everything valid before it is preserved; the checkpoint predates
    # every cut reservation, so canceled ELIGIBLE scenes are still in the pass
    # deck — released by construction, not by error-prone count subtraction. ---
    invalid = next((i for i, airing in enumerate(programs)
                    if airing["endEpochMs"] > now and airing["item"]["id"] not in index), None)
    if invalid is not None:
        programs = programs[:invalid]
        committed = min(committed, invalid)
        warnings.add("Upcoming airings changed: one or more scenes are no longer "
                     "available in this channel's source.")

    # --- rebuild triggers for the flexible future ---
    prior_index_map = {e["id"]: e for e in (old.get("index") or []) if isinstance(e, dict)}
    durations_changed = any(
        sid in prior_index_map and prior_index_map[sid].get("duration") != entry["duration"]
        for sid, entry in index.items()
    )
    policy_changed = old.get("policySignature") not in ("", None) and old.get("policySignature") != policy_sig
    deal_changed = old.get("sourceSignature") not in ("", None) and old.get("sourceSignature") != source_sig
    if (durations_changed or policy_changed or deal_changed) and len(programs) > committed:
        programs = programs[:committed]

    # --- membership deltas: additions enter the deck; genuinely NEW content
    # (createdAt not older than the index baseline; missing timestamps count
    # as new) also becomes a pending arrival. Old content that merely became
    # eligible joins the pass queue with no freshness promise. ---
    old_index_ids = old.get("indexIds")
    if isinstance(old_index_ids, list):
        added = set(index) - set(old_index_ids)
        removed = set(old_index_ids) - set(index)
        if added:
            baseline = checkpoint["arrivalBaseline"]
            for sid in sorted(added, key=lambda i: digest([channel["seed"], i])):
                if sid not in checkpoint["deck"]:
                    checkpoint["deck"].append(sid)
                created = _created_ms(index[sid].get("createdAt") or "")
                if created is None or created >= baseline:
                    checkpoint["pendingArrivals"].append({"id": sid, "firstSeenAt": now})

    checkpoint = prune_state(checkpoint, index)

    # --- encore recovery (R10): schedule expired — finish the airing clients
    # are already watching (from the UNPRUNED stored block), then resume. The
    # test for "expired" cannot rely on retained programs: a long outage
    # outruns the 48h retention window and leaves the retained list empty.
    # The encore is a committed airing like any other: folded exactly once by
    # the reservation replay / actual-history cursor, never inline here. ---
    schedule_expired = (not programs) or programs[-1]["endEpochMs"] <= now
    if schedule_expired:
        block = [a for a in encore_block if a.get("item", {}).get("id") in index]
        length = sum(a["endEpochMs"] - a["startEpochMs"] for a in block)
        if block and length > 0:
            offset = block[-1]["endEpochMs"] + ((now - block[-1]["endEpochMs"]) // length) * length
            for airing in block:
                end = offset + airing["endEpochMs"] - airing["startEpochMs"]
                if end > now:
                    programs.append({**airing, "startEpochMs": offset, "endEpochMs": end,
                                     "airingId": digest([channel["id"], offset, airing["item"]["id"]]),
                                     "block": "Encore"})
                    committed = len(programs)
                    degraded = True
                    break
                offset = end

    # --- durable state = checkpoint + committed reservations, replayed
    # through the one transition (exactly-once, both directions). The replay
    # starts at the actual-history cursor: airings the checkpoint already
    # consumed (end <= aired_through) must NOT be applied a second time. ---
    def _unconsumed(row):
        return row["endEpochMs"] > aired_through

    working = copy.deepcopy(checkpoint)
    for airing in programs[:committed]:
        if _unconsumed(airing):
            apply_airing(working, airing, channel, index, cfg)

    # --- arrival-driven flexible rebuild: when unplaced arrivals wait and a
    # credit is due, regenerate the flexible future so they land inside the
    # 24-72h target. A placed (retained) arrival holds still until it commits. ---
    if len(programs) > committed and working["arrivalCredits"] >= 1.0:
        placed = {a["item"]["id"] for a in programs[committed:] if a.get("arrival")}
        if any(a["id"] not in placed for a in working["pendingArrivals"]):
            programs = programs[:committed]
            working = copy.deepcopy(checkpoint)
            for airing in programs[:committed]:
                if _unconsumed(airing):
                    apply_airing(working, airing, channel, index, cfg)

    for airing in programs[committed:]:
        apply_airing(working, airing, channel, index, cfg)

    cursor = max(now, programs[-1]["endEpochMs"] if programs else now)

    relaxed_any = False
    while index and cursor < now + HORIZON and len(programs) < MAX_PROGRAMS:
        if not working["deck"]:
            _refill_pass(working, channel, index)
        sid, is_arrival, relaxed = pick_next(working, index, aired, cursor, tz, cfg)
        relaxed_any = relaxed_any or relaxed
        item = index[sid]
        end = cursor + max(1000, round(item["duration"] * 1000))
        airing = {"airingId": digest([channel["id"], cursor, sid]), "startEpochMs": cursor,
                  "endEpochMs": end, "item": item, "block": ""}
        if is_arrival:
            airing["arrival"] = True
        programs.append(airing)
        apply_airing(working, airing, None, None, cfg)
        cursor = end
    if relaxed_any:
        warnings.add("Some repeat, time-of-day, or spacing preferences could not be met by this lineup.")
    if cursor < now + HORIZON and index and len(programs) >= MAX_PROGRAMS:
        warnings.add("This source contains very short programs; the preparation limit was reached. "
                     "The scheduler will continue on its next run.")
    pending_ages = [now - a["firstSeenAt"] for a in checkpoint["pendingArrivals"]]
    if pending_ages and max(pending_ages) > 72 * HOUR:
        # Bulk imports drain at the configured share; report honestly instead
        # of promising a deadline the capacity cannot meet.
        warnings.add(f"{len(pending_ages)} new additions are waiting; at a "
                     f"{round(float(cfg['newShare']) * 100)}% share the oldest has waited "
                     f"{max(pending_ages) // HOUR}h and the rest drain gradually.")

    aired = prune_aired(aired, now)
    # The commit cursor is DERIVED from the retained timeline (R1): it can
    # never claim a boundary beyond the airings actually kept.
    if programs and committed:
        committed_through = max(a["startEpochMs"] for a in programs[:committed])
    else:
        committed_through = now
    # Unique scenes across the WHOLE published schedule (committed prefix +
    # flexible future) — the ops surface's variety stat. On a fresh build the
    # committed prefix is empty by design, so counting only reservations
    # would read 0 and hide the schedule that actually exists.
    scheduled_unique = len({a["item"]["id"] for a in programs})
    checkpoint_out = prune_state(checkpoint, index)
    generation = digest([source_sig, policy_sig, fingerprint, committed_through,
                         aired_through, digest(programs), digest(checkpoint_out)])
    publication = {
        "schema": SCHEMA,
        "channelId": channel["id"],
        "mode": MODE,
        "configuration": digest([source_sig, policy_sig]),
        "sourceSignature": source_sig,
        "policySignature": policy_sig,
        "programs": programs,
        "committedCount": committed,
        "committedThrough": committed_through,
        "airedThrough": aired_through,
        "preparedThrough": cursor,
        "sourceTotal": len(index),
        "scheduledUnique": scheduled_unique,
        "warnings": sorted(warnings),
        "version": digest(programs),
        "generation": generation,
        "generatedAt": now,
        "checkpoint": checkpoint_out,
        "aired": aired,
        "encoreBlock": [a for a in (encore_block if degraded else programs[-50:])],
        "index": entries,
        "indexIds": list(index),
        "indexFingerprint": fingerprint,
        "indexEpoch": epoch,
        "source": channel["source"],
        "degraded": degraded,
    }
    return publication


def read(data_dir: str | Path, channel_id: str) -> dict | None:
    """A continuing publication, or None when never published.

    Schema 3 validates strictly (R12): a corrupt program list or ledger
    raises — it is never silently normalized into a fresh editable state.
    A schema-2 file (the pre-remediation dev schema) reads loosely so the
    read surface keeps serving it; the next prepare migrates it to schema 3
    with a backup. A schema-1 file at a network path (a custom-engine
    artifact that can only exist on a hand-migrated dev box) reads through
    ``programming.read``.
    """
    path = programming.path(data_dir, channel_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    schema = data.get("schema") if isinstance(data, dict) else None
    if schema == SCHEMA:
        if not isinstance(data.get("programs"), list):
            raise ValueError("unreadable published schedule")
        validate_checkpoint(data.get("checkpoint"))
        for key in ("committedCount", "airedThrough", "preparedThrough"):
            if type(data.get(key)) is not int:
                raise ValueError(f"corrupt continuing state: {key}")
        return data
    if schema == 2:
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
        data = json.loads(_scheduler_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_scheduler(data_dir: str | Path, state: dict) -> None:
    programming.snapshots.write_json(_scheduler_path(data_dir), state)


def _index_due(prior: dict | None, signature: str, epoch: str, now: int, channel: dict) -> bool:
    if prior is None or prior.get("sourceSignature") != signature or prior.get("schema") != SCHEMA:
        return True
    # A cached EMPTY index is a successful result: it is reused until the TTL
    # like any other (an empty source must not monopolize the budget by being
    # "always due").
    if not isinstance(prior.get("index"), list):
        return True
    if prior.get("indexEpoch", "") != epoch:
        return True  # dynamic-source membership moved with its cutoff day
    ttl = INDEX_TTL + index_jitter(channel["id"])
    return now - int(prior.get("indexedAt", 0)) >= ttl


def _backup_v2(path: Path) -> None:
    """Keep the pre-migration schema-2 publication (rollback path)."""
    backup = path.with_name(path.name + ".v2.bak")
    if not backup.exists():
        shutil.copy2(path, backup)


#: Outcomes that mean real work was completed for a channel this run.
SUCCESS_OUTCOMES = frozenset({"ready", "recovered_from_outage", "indexed_empty"})
#: Named operational states that are neither success nor failure (R7).
DEFERRED_OUTCOMES = frozenset({"deferred_index_budget", "backoff",
                               "changed_during_build", "deactivated_during_build"})


def outcome_class(outcome: str) -> str:
    if outcome in SUCCESS_OUTCOMES:
        return "success"
    if outcome in DEFERRED_OUTCOMES:
        return "deferred"
    return "failure"


def prepare(client, data_dir, channel_id: str | None = None, now: int | None = None,
            budget: int = INDEX_BUDGET) -> dict:
    """Advance every scheduled network's publication. Per-channel failure
    isolation (build, ordering AND status reads — R6); one shared
    source-dedup cache; fair work rotation WITHIN priority classes so a
    bounded budget cannot always service the same first channels; cached
    empty indexes; a bounded consecutive-failure backoff; and a full-state
    commit guard with activation revalidation under the lock (R12)."""
    data_dir = Path(data_dir)
    now = now_ms() if now is None else now
    channels = active_channels(data_dir)
    if channel_id:
        channels = [ch for ch in channels if ch["id"] == channel_id]
    outcomes: dict[str, str] = {}
    if not channels:
        return {"channels": outcomes}

    # Ordering reads are isolated per channel (R6): one malformed publication
    # makes its own channel urgent; it never aborts the sort or its peers.
    def coverage_of(ch: dict):
        try:
            prior = read(data_dir, ch["id"])
        except Exception:
            return None
        if not isinstance(prior, dict) or prior.get("sourceSignature") != source_signature_of(ch):
            return None
        return int(prior.get("preparedThrough", 0))

    urgent, extending = [], []
    for ch in channels:
        cov = coverage_of(ch)
        if cov is None or cov <= now:
            urgent.append(ch)  # missing/mismatched/expired/corrupt: build now
        else:
            extending.append((cov, ch))
    extending.sort(key=lambda pair: pair[0])  # nearest expiry first
    extending = [ch for _, ch in extending]

    scheduler = _load_scheduler(data_dir)
    cursors = scheduler.get("cursors") if isinstance(scheduler.get("cursors"), dict) else {}
    failures = scheduler.get("failures") if isinstance(scheduler.get("failures"), dict) else {}

    def rotate(rows: list, cls: int) -> list:
        if not rows:
            return rows
        cur = int(cursors.get(str(cls), 0)) % len(rows)
        return rows[cur:] + rows[:cur]

    ordered = [(0, ch) for ch in rotate(urgent, 0)] + [(1, ch) for ch in rotate(extending, 1)]
    serviced = {0: 0, 1: 0}

    source_cache: dict = {}
    indexed = 0
    for cls, channel in ordered:
        cid = channel["id"]
        try:
            rec = failures.get(cid)
            if isinstance(rec, dict) and rec.get("count", 0) >= FAILURE_BACKOFF_LIMIT \
                    and now - int(rec.get("lastAt", 0)) < FAILURE_BACKOFF:
                outcomes[cid] = "backoff"
                continue
            signature = source_signature_of(channel)
            prior = read(data_dir, cid)
            if isinstance(prior, dict) and prior.get("schema") == 2:
                _backup_v2(programming.path(data_dir, cid))
            due = _index_due(prior, signature,
                             lineup.effective_epoch(channel["source"], today=_utc_date(now)),
                             now, channel)
            if due and indexed >= budget:
                outcomes[cid] = "deferred_index_budget"
                continue
            if due:
                entries = index_source(client, channel, source_cache)
                indexed += 1
                indexed_at = now
            else:
                entries = prior["index"]
                indexed_at = int(prior.get("indexedAt", now))
            # Pass the real prior at ANY schema: build()'s migration branch
            # reconstructs from the client-visible timeline, so a schema-2
            # file's still-airing program survives instead of being reset.
            publication = build(channel, entries,
                                prior if isinstance(prior, dict) else None,
                                now)
            publication["indexedAt"] = indexed_at
            outcomes[cid] = "indexed_empty" if not publication["programs"] else (
                "recovered_from_outage" if publication["degraded"] else "ready")
            # Commit guard: membership, policy, playable state, activation, or
            # a sibling writer may have moved under us during indexing; never
            # overwrite a newer generation. The guard token covers the WHOLE
            # durable state (programs + ledger + cursors), not just the
            # program digest, and runs under the store's ACTUAL writer lock
            # (library deployments: .library.lock — audit C6).
            prior_generation = prior.get("generation") if isinstance(prior, dict) else None
            with writer_lock_for(data_dir):
                from justwatch import channel_service
                rollout = try_load_rollout(data_dir)
                latest = next(
                    (row for row in network_rows(data_dir) if row["id"] == cid), None)
                state_ok = True
                if channel_service.library_active(data_dir):
                    lib_channel = channel_service.get_channel(data_dir, cid)
                    state_ok = lib_channel is not None \
                        and bool(lib_channel.get("enabled", True)) \
                        and not bool(lib_channel.get("archived")) \
                        and not bool(lib_channel.get("paused"))
                if latest is None or not state_ok \
                        or scheduled_mode(latest, rollout) != MODE \
                        or source_signature_of(latest) != signature \
                        or policy_signature_of(latest) != policy_signature_of(channel):
                    outcomes[cid] = "deactivated_during_build"
                    continue
                current = read(data_dir, cid)
                if current is not None and current.get("generation") != prior_generation:
                    outcomes[cid] = "changed_during_build"
                    continue
                programming.snapshots.write_json(programming.path(data_dir, cid), publication)
            serviced[cls] += 1
            failures.pop(cid, None)
        except Exception as exc:  # one channel must never block the tier
            outcomes[cid] = f"{type(exc).__name__}: {exc}"
            record = failures.get(cid)
            count = record.get("count", 0) + 1 if isinstance(record, dict) else 1
            failures[cid] = {"count": count, "lastAt": now}

    _write_scheduler(data_dir, {
        "cursors": {str(cls): int(cursors.get(str(cls), 0)) + serviced[cls] for cls in (0, 1)},
        "failures": failures,
    })
    return {"channels": outcomes}


def catalog_lock_for(data_dir):
    return catalog.catalog_lock(data_dir, timeout=30.0)


def writer_lock_for(data_dir):
    """The lock that actually guards the authoritative store: library
    deployments serialize on .library.lock (Apply, the refresh journal, and
    every publication commit); pre-migration keeps the catalog lock."""
    from justwatch import channel_service
    return channel_service.writer_lock(data_dir, timeout=30.0)


# ---------------------------------------------------------------------------
# Status manifest: the lightweight surface Directory/Desk/ops read
# ---------------------------------------------------------------------------


def status_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "programming" / "status.json"


def read_manifest(data_dir: str | Path) -> dict:
    """The STORED manifest: durable facts only (no derived freshness)."""
    try:
        data = json.loads(status_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def channel_status(publication: dict | None, mode: str) -> dict:
    """Durable per-channel facts stored in the manifest. Coverage/expiry are
    deliberately NOT stored (R7): they are derived at READ time by
    :func:`status_view`, so they age truthfully without a scheduler write."""
    if not publication:
        return {"mode": mode, "published": False}
    return {
        "mode": mode,
        "published": True,
        "empty": not publication.get("programs"),
        "version": publication.get("version"),
        "generatedAt": publication.get("generatedAt"),
        "preparedThrough": int(publication.get("preparedThrough", 0)),
        "sourceTotal": publication.get("sourceTotal"),
        "scheduledUnique": publication.get("scheduledUnique"),
        "degraded": bool(publication.get("degraded")),
        "warnings": publication.get("warnings", []),
        "indexedAt": publication.get("indexedAt"),
    }


def status_view(manifest: dict, now: int) -> dict:
    """Live read-side projection (R7): coverage, expiry and readiness are
    computed from stored timestamps against the READER'S clock — sync reads
    stay lightweight (arithmetic on the manifest, never schedule files) and
    stale manifests report stale truth instead of frozen coverage."""
    now = int(now)
    channels: dict[str, dict] = {}
    for cid, entry in (manifest.get("channels") or {}).items():
        if not isinstance(entry, dict):
            continue
        view = dict(entry)
        prepared_through = int(entry.get("preparedThrough", 0))
        coverage = max(0.0, round((prepared_through - now) / HOUR, 1))
        view["coverageHours"] = coverage
        view["ready"] = bool(entry.get("published")) and coverage > 0
        # Alert threshold: a healthy hourly scheduler never lets ready coverage
        # drop below 24h; anything below is one missed run from encore.
        view["expiring"] = view["ready"] and coverage < 24
        view["encore"] = bool(entry.get("degraded"))
        channels[cid] = view
    return {
        "schema": manifest.get("schema"),
        "generatedAt": manifest.get("generatedAt"),
        "lastRun": manifest.get("lastRun"),
        "now": now,
        "channels": channels,
    }


def read_status(data_dir: str | Path, now: int | None = None) -> dict:
    """The live status view (read-only): last run + per-channel publication
    status with freshness derived at read time."""
    return status_view(read_manifest(data_dir), now_ms() if now is None else now)


def write_status(data_dir: str | Path, run: dict, now: int | None = None) -> dict:
    """Atomically publish the lightweight status manifest: per-channel durable
    facts (custom + network) plus the last run's correlated outcomes, so
    Directory and ops tooling never parse large schedule files. Entries for
    channels that are no longer scheduled (deactivated rollout, fixed-mode
    edit) are dropped — stale "continuing" rows would lie about what airs."""
    data_dir = Path(data_dir)
    now = now_ms() if now is None else now
    channels: dict[str, dict] = {}
    try:
        from justwatch import channel_service
        if channel_service.library_active(data_dir):
            customs = channel_service.custom_channels_legacy(data_dir)
        else:
            customs = catalog.load(data_dir).get("channels", [])
        for channel in customs:
            mode = programming.policy(channel.get("programming"))["mode"]
            if mode == "fixed":
                continue
            try:
                prior = programming.read(data_dir, channel["id"])
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                channels[channel["id"]] = {"mode": mode, "published": False, "error": str(exc)[:200]}
                continue
            channels[channel["id"]] = channel_status(prior, mode)
    except (catalog.CatalogError, LibraryError):
        pass  # a corrupt store must not take the status manifest down
    for channel in scheduled_channels_safe(data_dir):
        cid = channel["id"]
        try:
            prior = read(data_dir, cid)
            channels[cid] = channel_status(prior, MODE)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # Per-channel isolation (R6): a corrupt publication degrades its
            # own row; the manifest (and every peer) still publishes.
            channels[cid] = {"mode": MODE, "published": False, "error": str(exc)[:200]}
    manifest = {
        "schema": 2,
        "generatedAt": now,
        "lastRun": run,
        "channels": channels,
    }
    programming.snapshots.write_json(status_path(data_dir), manifest)
    return manifest


def scheduled_channels_safe(data_dir: str | Path) -> list[dict]:
    try:
        return scheduled_channels(data_dir)
    except (ValueError, networks.NetworksError):
        return []


#: Read-path alias: sync surfaces must never fail over a broken rollout.
active_channels_safe = scheduled_channels_safe


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
        "rollout": {"enabled": rollout["enabled"], "stage": rollout.get("stage", "active"),
                    "active": total},
        "total": total,
        "offset": offset,
        "channels": rows[offset:offset + max(1, min(200, limit))],
    }
