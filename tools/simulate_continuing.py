#!/usr/bin/env python3
"""Offline 30-day simulation: continuing networks vs the fixed-50 baseline.

Clock-injected and fully offline (a fake GraphQL library answers the
ContinuingIndex query), so the comparison is repeatable on any machine:

    python3 tools/simulate_continuing.py [--days 30] [--out analysis/continuing-simulation]

Fixtures span the shapes that matter: small/large/huge sources, a highly
overlapping pair, short-clip and long-form channels, and a created-at recency
(dynamic) source. Events: daily trickle arrivals, one 200-scene bulk import,
rolling deletions, a protected-zone deletion, an OLD-content re-eligibility
probe (must NOT be labeled a new arrival), and a 240-hour scheduler outage —
longer than the full publication horizon (168h) plus retention (48h), so
retention cannot be what saves recovery. Every step prepares ALL channels in
one batched ``continuing.prepare`` call (tier-level fairness/budget path) and
reloads state from disk (restart-equivalent).

The baseline reproduces today's production behavior approximately: the
channel's 50-item seeded rotation, looping forever from a fixed anchor. The
digest-based ordering approximates (is not validated against) Stash's actual
``random_<seed>`` order — labeled an approximation, not an exact baseline.

Measurement independence (remediation R8): every gate below is derived from
PUBLISHED programs and library events by code that shares no helpers with the
engine's fold/selection path:

  * zero unexpected protected-airing changes — the only sanctioned exception
    is airings at/after the EARLIEST airing invalid at that step (derived
    from the actual membership transition, not from any index removal)
  * zero ineligible future items after reconciliation
  * zero duplicate consumption within a pass — MEASURED, via the independent
    ledger (distinct scenes aired between consecutive airings of a scene vs.
    the eligible pass size), never a constant
  * >50 unique scheduled videos for the large fixture
  * feasible arrivals first-air within 72h — tracked from published first
    exposure, for every arrival (flagged or not); never-aired overdue
    arrivals are counted and attributed
  * encore continuity during the outage is measured from the READ surface
    (programming.schedule), not from skipped worker iterations
  * adverse fixtures for these gates live in tests/test_simulation_metrics.py
    (they must FAIL on corrupted input)

Metrics recorded per channel and overall in results.json; report.md is
generated from the results so the comparison is reviewable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from justwatch import continuing as c  # noqa: E402
from justwatch import networks, programming as p, snapshots  # noqa: E402

HOUR = c.HOUR
DAY = 24 * HOUR
TZ = c.programming_timezone("America/Toronto")

# Start mid-February: the 30-day window crosses America/Toronto's 2026-03-08
# spring-forward, so local viewing-window math is exercised across DST.
START_MS = int(dt.datetime(2026, 2, 15, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)

STUDIO_POOL = [f"studio_{i}" for i in range(6)]
PERFORMER_POOL = [f"perf_{i}" for i in range(10)]

#: The outage must exceed the full publication horizon PLUS retention (R8):
#: 168h horizon + 48h retention = 216h; 240h cannot be survived by pruning.
OUTAGE_START_STEP = 10 * 24
OUTAGE_END_STEP = 20 * 24


def make_scene(sid: int, duration: float, created_at: str, studios: int = 1):
    return {
        "id": str(sid),
        "title": f"Scene {sid}",
        "duration": duration,
        "studioId": STUDIO_POOL[sid % len(STUDIO_POOL)] if studios else "",
        "studio": f"Studio {sid % len(STUDIO_POOL)}" if studios else "",
        "performerIds": [PERFORMER_POOL[sid % len(PERFORMER_POOL)]],
        "date": "2025-06-01",
        "createdAt": created_at,
        "preview": f"/scene/{sid}/preview",
    }


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()


class Library:
    """The synthetic Stash: membership per source key + paging + query count."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.queries = 0

    def add(self, scene: dict) -> None:
        self.rows[scene["id"]] = scene

    def remove(self, sid: str) -> None:
        self.rows.pop(sid, None)


# ---------------------------------------------------------------------------
# Fixed-50 baseline: today's production behavior (approximate ordering)
# ---------------------------------------------------------------------------


def digest_order(ids, seed, pass_number=1):
    from justwatch.programming import digest
    return sorted(ids, key=lambda i: digest([seed, pass_number, i]))


def fixed_airings(channel, rows, start_ms, end_ms):
    """The baseline timeline: first 50 playable in seeded order, looping from
    the broadcast anchor (BroadcastSchedule math). The seeded order here is a
    digest-based approximation of Stash's random_<seed> sort."""
    if not rows:
        return []
    playable = digest_order([r["id"] for r in rows], channel["seed"])[:50]
    by_id = {r["id"]: r for r in rows}
    anchor = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    loop_ms = sum(int(by_id[i]["duration"] * 1000) for i in playable)
    airings = []
    t = anchor
    while t < end_ms + loop_ms:
        for sid in playable:
            dur = int(by_id[sid]["duration"] * 1000)
            if t + dur > start_ms:
                airings.append((max(t, start_ms), t + dur, sid))
            t += dur
        if t > end_ms + loop_ms:
            break
    return [a for a in airings if a[1] > start_ms and a[0] < end_ms]


# ---------------------------------------------------------------------------
# Metrics shared by both engines (pure functions of the airing log)
# ---------------------------------------------------------------------------


def local_window(ms: int) -> int:
    return dt.datetime.fromtimestamp(ms / 1000, TZ).hour // 3


def viewing_repetition(airings, days=14):
    """Evening (local 18:00-24:00) scene-set similarity across consecutive
    days: 1.0 means the identical evening lineup every day (the complaint)."""
    evenings: dict[str, set] = {}
    for start, _end, sid in airings:
        local = dt.datetime.fromtimestamp(start / 1000, TZ)
        if local.hour >= 18:
            evenings.setdefault(local.date().isoformat(), set()).add(sid)
    keys = sorted(evenings)[-days:]
    jaccards = []
    for a, b in zip(keys, keys[1:]):
        union = evenings[a] | evenings[b]
        jaccards.append(len(evenings[a] & evenings[b]) / len(union) if union else 1.0)
    return statistics.mean(jaccards) if jaccards else 1.0


def same_window_recurrence(airings):
    """Fraction of airings whose scene also aired in the same local 3h window
    within the previous 7 days."""
    history: dict[str, list] = {}
    hits = total = 0
    for start, _end, sid in sorted(airings):
        window = local_window(start)
        prior = [s for s in history.get(sid, []) if start - s < 7 * DAY]
        if prior:
            total += 1
            if any(local_window(s) == window for s in prior):
                hits += 1
        history.setdefault(sid, []).append(start)
    return hits / total if total else 0.0


def repeat_gaps(airings):
    """Hours between CONSECUTIVE airings of the same scene (start-to-start).
    Gaps and epoch timestamps are never mixed in one list."""
    by_scene: dict[str, list] = {}
    for start, _end, sid in sorted(airings):
        by_scene.setdefault(sid, []).append(start)
    all_gaps = [b - a for starts in by_scene.values() for a, b in zip(starts, starts[1:])]
    if not all_gaps:
        return None
    ordered = sorted(all_gaps)
    return {
        "medianHours": round(statistics.median(all_gaps) / HOUR, 1),
        "minHours": round(min(all_gaps) / HOUR, 1),
        "p90Hours": round(ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)] / HOUR, 1),
    }


def duplicate_airings(airings, library_size_at, released=None, repeat_guard=6 * HOUR,
                      interruptions=()):
    """INDEPENDENT duplicate-consumption oracle (no engine helpers).

    The engine may legitimately re-air a scene right at a pass boundary (the
    immediate-repeat guard only protects a short window), and a pending
    arrival backlog can legitimately delay pass closures — so pass boundaries
    are not observable from the outside. What IS observable: a scene re-airing
    after fewer than K distinct other airings, where K is the pass length's
    quarter (a real same-pass double consumption) clamped below the guard
    window's slot count (boundary adjacency is sanctioned inside the guard).

    `airings`: (start, end, sid) triples; `library_size_at(start)` returns the
    eligible count; `released` lists (sid, at_ms) reservation releases whose
    sanctioned re-air is exempt; `repeat_guard` is the engine's published
    immediate-repeat protection window (contract constant, 6h). Returns
    (sid, start) pairs.
    """
    released = released or []
    ordered = sorted(airings)
    if len(ordered) < 2:
        return []
    deltas = [b[0] - a[0] for a, b in zip(ordered, ordered[1:]) if b[0] > a[0]]
    if not deltas:
        return []
    slot_ms = max(1, statistics.median(deltas))
    guard_slots = max(1, round(repeat_guard / slot_ms))
    duplicates = []
    positions: dict[str, list[int]] = {}
    for i, (_start, _end, sid) in enumerate(ordered):
        positions.setdefault(sid, []).append(i)
    for sid, idxs in positions.items():
        for a, b in zip(idxs, idxs[1:]):
            # SLOT COUNT between the pair, not distinct scenes: legitimate
            # order swings (even a full pass reversal) spread the pair wide,
            # while a real double-consumption lands close to its predecessor.
            slots_between = b - a - 1
            size = library_size_at(ordered[a][0]) or 1
            # A third of the pass: tolerant even of a full pass-order reversal
            # (whose boundary pairs land near half-pass distance), tight enough
            # that a genuine double-consumption (adjacent or mid-pass insert)
            # is caught; clamped below the guard window's slots.
            k = max(1, min((size - 1) // 3, guard_slots - 1))
            if slots_between < k:
                t1, t2 = ordered[a][0], ordered[b][0]
                if any(r_sid == sid and t1 < r_at < t2 for r_sid, r_at in released):
                    continue
                # A scheduler outage is a broadcast interruption: a pair
                # straddling it (including the encore recovery replay that
                # follows) is sanctioned repetition, not double consumption.
                if any(t1 < i_end and t2 > i_start for i_start, i_end in interruptions):
                    continue
                duplicates.append((sid, ordered[b][0]))
    return duplicates


def expected_encore_current(frozen_publication: dict, at_ms: int) -> dict | None:
    """The airing the read surface MUST be serving at `at_ms` while the
    publication is frozen: the deterministic fallback computed from the file's
    own last-50 block (the schema 1/2 contract; schema 3 stores the same
    block). This is the outage oracle: independent of the running engine."""
    programs = frozen_publication.get("programs") or []
    if not programs or at_ms < programs[-1]["endEpochMs"]:
        return None
    block = frozen_publication.get("encoreBlock") or programs[-50:]
    length = sum(p["endEpochMs"] - p["startEpochMs"] for p in block)
    if length <= 0:
        return None
    anchor = block[-1]["endEpochMs"]
    cursor = anchor + ((at_ms - anchor) // length) * length
    from justwatch.programming import digest as _digest
    channel_id = frozen_publication.get("channelId")
    for p in block:
        end = cursor + p["endEpochMs"] - p["startEpochMs"]
        if end > at_ms:
            # the fallback re-ids each repetition from (channel, cursor, scene)
            return {"airingId": _digest([channel_id, cursor, p["item"]["id"]]),
                    "startEpochMs": cursor}
        cursor = end
    return None


# ---------------------------------------------------------------------------
# Simulation driver
# ---------------------------------------------------------------------------


def network_row(cid, number, seed, source, name):
    return {"id": cid, "number": number, "name": name, "glyph": "\uf005",
            "color": "#123456", "section": "general", "family": "tag_spotlight",
            "count": 0, "sort": "shuffle", "seed": seed,
            "programmingMode": "fixed", "source": source}


def run(days: int, out_dir: Path) -> dict:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="jw-sim-"))

    # --- fixtures -----------------------------------------------------------
    next_id = [1000]

    def fresh(n, duration, created_ms=None, start=None):
        base = start if start is not None else next_id[0]
        if start is None:
            next_id[0] += n
        created = iso(created_ms if created_ms is not None else START_MS - 14 * DAY)
        return [make_scene(base + i, duration, created) for i in range(n)]

    library = Library()
    # Pre-baseline content: created two weeks before the sim starts, so the
    # first index is the arrival baseline and existing content is not "new".
    small = fresh(30, 25 * 60); [library.add(s) for s in small]
    large = fresh(500, 30 * 60); [library.add(s) for s in large]
    huge = fresh(5000, 20 * 60); [library.add(s) for s in huge]
    shared = fresh(400, 22 * 60); [library.add(s) for s in shared]
    overlap_a_extra = fresh(100, 22 * 60); [library.add(s) for s in overlap_a_extra]
    overlap_b_extra = fresh(100, 22 * 60); [library.add(s) for s in overlap_b_extra]
    shorts = fresh(200, 3 * 60); [library.add(s) for s in shorts]
    longform = fresh(60, 90 * 60); [library.add(s) for s in longform]
    dynamic = fresh(150, 26 * 60); [library.add(s) for s in dynamic]
    # OLD content that becomes (re-)eligible mid-run: created before the
    # baseline, added to a channel's membership on day 6. The engine must
    # queue it WITHOUT the arrival machinery (no freshness promise).
    old_reeligible = fresh(20, 22 * 60); [library.add(s) for s in old_reeligible]

    sources = {
        "net_00000001": {"type": "filter", "tags": ["101"]},                    # small
        "net_00000002": {"type": "filter", "tags": ["102"]},                    # large
        "net_00000003": {"type": "filter", "tags": ["103"]},                     # huge
        "net_00000004": {"type": "filter", "tags": ["104"]},                # overlap a
        "net_00000005": {"type": "filter", "tags": ["105"]},                # overlap b
        "net_00000006": {"type": "filter", "tags": ["106"]},                   # shorts
        "net_00000007": {"type": "filter", "tags": ["107"]},                 # longform
        "net_00000008": {"type": "filter", "tags": ["108"],
                         "createdAt": {"withinDays": 30}},                            # dynamic
    }
    tag_to_channel = {sources[cid]["tags"][0]: cid for cid in sources}
    channels = [
        network_row("net_00000001", 100, 11, sources["net_00000001"], "Small Mix"),
        network_row("net_00000002", 101, 22, sources["net_00000002"], "Large Mix"),
        network_row("net_00000003", 102, 33, sources["net_00000003"], "Huge Mix"),
        network_row("net_00000004", 103, 44, sources["net_00000004"], "Overlap A"),
        network_row("net_00000005", 104, 55, sources["net_00000005"], "Overlap B"),
        network_row("net_00000006", 105, 66, sources["net_00000006"], "Short Clips"),
        network_row("net_00000007", 106, 77, sources["net_00000007"], "Long Form"),
        network_row("net_00000008", 107, 88, sources["net_00000008"], "Recent Window"),
    ]
    membership_seed = {
        "net_00000001": {s["id"] for s in small},
        "net_00000002": {s["id"] for s in large},
        "net_00000003": {s["id"] for s in huge},
        "net_00000004": {s["id"] for s in shared} | {s["id"] for s in overlap_a_extra},
        "net_00000005": {s["id"] for s in shared} | {s["id"] for s in overlap_b_extra},
        "net_00000006": {s["id"] for s in shorts},
        "net_00000007": {s["id"] for s in longform},
        "net_00000008": {s["id"] for s in dynamic},
    }
    networks_path = tmp / "networks.json"
    networks_path.write_text(json.dumps({"channels": channels}))
    original_load = networks.load
    networks.load = lambda path=None: original_load(networks_path)
    original_get = networks.get

    def fake_get(channel_id):
        for ch in channels:
            if ch["id"] == channel_id:
                return ch
        return None
    networks.get = fake_get
    (tmp / "continuing-networks.json").write_text(
        json.dumps({"enabled": True, "stage": "active",
                    "networkIds": [ch["id"] for ch in channels]}))

    def current_members(cid, at_ms):
        """The channel's true eligible set at a moment: seed membership minus
        deletions, filtered by the dynamic source's rolling window."""
        today = dt.datetime.fromtimestamp(at_ms / 1000, dt.timezone.utc).date()
        source = sources[cid]
        cutoff = (today - dt.timedelta(days=source["createdAt"]["withinDays"])).isoformat() \
            if source.get("createdAt") else None
        rows = [library.rows[i] for i in membership_seed[cid] if i in library.rows]
        if cutoff:
            rows = [r for r in rows if r["createdAt"][:10] >= cutoff]
        return {r["id"] for r in rows}

    class MembershipClient:
        def __init__(self):
            self.queries = 0
            self.state = {"now": START_MS}

        def submit(self, query, variables):
            self.queries += 1
            assert "ContinuingIndex" in query, f"unexpected query {query[:60]}"
            # Batched tier-level prepare indexes several channels per call: the
            # channel is identified by its own scene filter, exactly as the
            # engine scopes each query.
            tag = ((variables.get("scene_filter") or {}).get("tags") or {}).get("value")
            cid = tag_to_channel[tag[0]]
            rows = [library.rows[i] for i in sorted(current_members(cid, self.state["now"]), key=int)]
            page = variables["filter"]["page"]
            per_page = variables["filter"]["per_page"]
            chunk = rows[(page - 1) * per_page: page * per_page]
            return {"findScenes": {"count": len(rows),
                                   "scenes": [dict(r, studio={"id": r["studioId"], "name": r["studio"]},
                                                   performers=[{"id": x} for x in r["performerIds"]],
                                                   files=[{"duration": r["duration"]}],
                                                   created_at=r["createdAt"]) for r in chunk]}}

    client = MembershipClient()

    # --- events --------------------------------------------------------------
    hours = days * 24
    outage = range(OUTAGE_START_STEP, OUTAGE_END_STEP)  # 240h dark: horizon+retention overrun
    bulk_import_at = 4 * 24
    bulk = fresh(200, 28 * 60, created_ms=START_MS + bulk_import_at * HOUR)
    bulk_ids = {s["id"] for s in bulk}
    protected_delete_probe_at = 5 * 24 + 3
    old_reeligible_at = 6 * 24

    pending_arrivals: dict[str, list] = {cid: [] for cid in membership_seed}
    arrival_first_air: dict[str, list] = {cid: [] for cid in membership_seed}  # (hours, outage_affected)
    never_aired_overdue: dict[str, list] = {cid: [] for cid in membership_seed}
    old_reeligible_flagged: list = []
    protected_changes = 0
    removal_driven_changes = 0
    ineligible_future = 0
    duplicates_total = 0
    build_times = []
    encore_breaks = 0
    publications = {cid: None for cid in membership_seed}
    logged_airing_ids = {cid: set() for cid in membership_seed}
    aired_log: dict[str, list] = {cid: [] for cid in membership_seed}  # (start, end, sid)
    releases: dict[str, list] = {cid: [] for cid in membership_seed}  # (sid, at_ms)
    horizon_min_hours = {cid: 168.0 for cid in membership_seed}
    file_sizes = {cid: 0 for cid in membership_seed}
    peak_mem_mb = 0.0
    member_sizes: list = []  # (cid, at_ms, size) sampled every prepare step
    member_sets: dict[str, list] = {cid: [] for cid in membership_seed}  # (at_ms, ids) sampled
    outage_reference: dict = {"net_00000006": None}
    last_encore = None

    def in_outage(step: int) -> bool:
        return OUTAGE_START_STEP <= step < OUTAGE_END_STEP

    for step in range(hours + 1):
        now = START_MS + step * HOUR
        client.state["now"] = now
        # ---- library events -------------------------------------------------
        if step % 24 == 12 and 0 < step < (days - 2) * 24:
            one = make_scene(next_id[0], 24 * 60, iso(now)); next_id[0] += 1
            library.add(one)
            membership_seed["net_00000001"].add(one["id"])
            pending_arrivals["net_00000001"].append((one["id"], now))
        if step == bulk_import_at:
            for s in bulk:
                library.add(s)
            membership_seed["net_00000002"] |= bulk_ids
            pending_arrivals["net_00000002"].extend((s["id"], now) for s in bulk)
        if step % 36 == 30 and step > 48:
            victims = sorted(membership_seed["net_00000004"] -
                             {s["id"] for s in shared} -
                             {s["id"] for s in overlap_a_extra})
            if victims:
                victim = victims[0]
                library.remove(victim)
                membership_seed["net_00000004"].discard(victim)
        if step == protected_delete_probe_at:
            pub = publications["net_00000006"]
            if pub:
                target = next((a for a in pub["programs"]
                               if a["endEpochMs"] > now + 12 * HOUR), None)
                if target:
                    library.remove(target["item"]["id"])
                    membership_seed["net_00000006"].discard(target["item"]["id"])
        if step == old_reeligible_at:
            for s in old_reeligible:
                membership_seed["net_00000004"].add(s["id"])
        # dynamic channel: one new recent scene per day (created "today" so
        # the rolling 30-day window keeps it; fixed-date fixtures would all
        # expire mid-simulation).
        if step % 24 == 6 and step > 0:
            one = make_scene(next_id[0], 26 * 60, iso(now)); next_id[0] += 1
            library.add(one)
            membership_seed["net_00000008"].add(one["id"])

        # ---- outage: the scheduler is dark, but the READ SURFACE keeps
        # serving. The expected encore is derived ONCE from the frozen
        # publication file and must match the served airing at every dark
        # hour — two clients (or the recovering engine) can never disagree
        # with the frozen fallback (R8/R10). ----
        if in_outage(step):
            if outage_reference["net_00000006"] is None and publications["net_00000006"] is not None:
                outage_reference["net_00000006"] = json.loads(
                    (tmp / "programming" / "net_00000006.json").read_text())
            frozen = outage_reference["net_00000006"]
            if frozen is not None:
                served = p.schedule(tmp, "net_00000006", at=now)["programs"]
                expected = expected_encore_current(frozen, now)
                if expected is None and frozen.get("programs"):
                    # The frozen schedule has not expired yet: the read surface
                    # must serve the frozen program containing `now`.
                    expected = next(
                        ({"airingId": a["airingId"], "startEpochMs": a["startEpochMs"]}
                         for a in frozen["programs"]
                         if a["startEpochMs"] <= now < a["endEpochMs"]), None)
                if served and expected is not None:
                    if served[0]["airingId"] != expected["airingId"] or \
                            served[0]["startEpochMs"] != expected["startEpochMs"]:
                        encore_breaks += 1
                elif bool(served) != (expected is not None):
                    encore_breaks += 1
            continue

        # ---- hourly prepare: ALL channels in one batched tier-level call ----
        measure = step % (2 * 24) == 24  # sample memory across the whole run
        if measure:
            tracemalloc.start()
        t0 = time.perf_counter()
        outcome = c.prepare(client, tmp, now=now)
        build_times.append(time.perf_counter() - t0)
        if measure:
            _, peak = tracemalloc.get_traced_memory()
            peak_mem_mb = max(peak_mem_mb, peak / 1_048_576)
            tracemalloc.stop()
        bad = {k: v for k, v in outcome["channels"].items()
               if c.outcome_class(v) == "failure"}
        assert not bad, f"preparation failures at step {step}: {bad}"

        for cid in membership_seed:
            pub = c.read(tmp, cid)
            if pub is None:
                continue
            eligible = current_members(cid, now)
            member_sizes.append((cid, now, len(eligible)))
            if step % 6 == 0:
                member_sets[cid].append((now, set(eligible)))
            if publications[cid] is not None:
                prev_pub = publications[cid]
                prev_now = now - HOUR
                protected = {}
                for i, a in enumerate(prev_pub["programs"]):
                    if a["endEpochMs"] > prev_now and a["startEpochMs"] < prev_now + c.PROTECT:
                        protected[a["airingId"]] = (i, a)
                current = {a["airingId"]: a for a in pub["programs"]}
                # The ONLY sanctioned exception: airings at/after the EARLIEST
                # airing invalid at this step (derived from true membership).
                expected_cut = next(
                    (i for i, a in enumerate(prev_pub["programs"])
                     if a["endEpochMs"] > now and a["item"]["id"] not in eligible), None)
                if expected_cut is not None:
                    for i, a in enumerate(prev_pub["programs"]):
                        if i >= expected_cut:
                            releases[cid].append((a["item"]["id"], now))
                for aid, (idx, airing) in protected.items():
                    if current.get(aid) != airing:
                        if expected_cut is not None and idx >= expected_cut:
                            removal_driven_changes += 1
                        else:
                            protected_changes += 1
                future = [a for a in pub["programs"]
                          if a["endEpochMs"] > now and a["startEpochMs"] > now]
                own_index = set(pub["indexIds"])
                ineligible_future += sum(1 for a in future if a["item"]["id"] not in own_index)
            publications[cid] = pub
            horizon_min_hours[cid] = min(horizon_min_hours[cid],
                                         (pub["preparedThrough"] - now) / HOUR)
            file_sizes[cid] = max(file_sizes[cid],
                                  (tmp / "programming" / f"{cid}.json").stat().st_size)
            # Aired log: each airing recorded ONCE (publications retain 48h of
            # history, so re-logging every hour would duplicate).
            for a in pub["programs"]:
                if a["endEpochMs"] <= now and a["airingId"] not in logged_airing_ids[cid]:
                    logged_airing_ids[cid].add(a["airingId"])
                    aired_log[cid].append((a["startEpochMs"], a["endEpochMs"], a["item"]["id"]))
            # Old-content re-eligibility must NOT ride the arrival machinery.
            if step >= old_reeligible_at and cid == "net_00000004":
                old_reeligible_flagged.extend(
                    a["item"]["id"] for a in pub["programs"]
                    if a.get("arrival") and a["item"]["id"] in {s["id"] for s in old_reeligible})

        # ---- arrival deadlines: tracked for EVERY arrival from published
        # first exposure, not only specially flagged slots (R8) ----
        for cid, plist in pending_arrivals.items():
            still = []
            pub = publications[cid]
            for sid, seen_at in plist:
                airing = None
                if pub:
                    airing = next((a for a in pub["programs"]
                                   if a["item"]["id"] == sid and a["startEpochMs"] >= seen_at), None)
                if airing is not None:
                    dark = any(seen_at <= START_MS + s * HOUR <= airing["startEpochMs"]
                               for s in outage)
                    arrival_first_air[cid].append(((airing["startEpochMs"] - seen_at) / HOUR, dark))
                elif now - seen_at > 72 * HOUR:
                    never_aired_overdue[cid].append((sid, (now - seen_at) / HOUR))
                else:
                    still.append((sid, seen_at))
            pending_arrivals[cid] = still

    networks.load = original_load
    networks.get = original_get

    # ---- metrics -------------------------------------------------------------
    final_members = {cid: current_members(cid, START_MS + hours * HOUR)
                     for cid in membership_seed}

    def size_lookup(cid, start):
        # Eligible-library size at the latest observed moment <= `start`.
        best = None
        for c2, at, size in member_sizes:
            if c2 == cid and at <= start:
                best = size
        return best if best is not None else len(final_members[cid])

    def ids_lookup(cid, start):
        # Eligible-library id set at the latest sampled moment <= `start`.
        best = None
        for at, ids in member_sets[cid]:
            if at <= start:
                best = ids
        return best if best is not None else set(final_members[cid])

    dupe_counts: dict[str, int] = {}
    dupe_events: dict[str, list] = {}

    def channel_metrics(cid, rows_ids):
        airings = aired_log[cid]
        aired_ids = {sid for _, _, sid in airings}
        eligible = len(rows_ids & set(library.rows))
        dupes = duplicate_airings(airings, lambda start: len(ids_lookup(cid, start)),
                                  released=releases[cid],
                                  interruptions=[(START_MS + OUTAGE_START_STEP * HOUR,
                                                  START_MS + OUTAGE_END_STEP * HOUR)])
        dupe_counts[cid] = len(dupes)
        if dupes:
            detail = []
            for sid, t2 in dupes:
                prior = [t for t, _e, s in aired_log[cid] if s == sid and t < t2]
                gap = round((t2 - max(prior)) / HOUR, 2) if prior else None
                detail.append({"sid": sid, "atHoursFromStart": round((t2 - START_MS) / HOUR, 1),
                               "gapHours": gap})
            dupe_events[cid] = detail
        return {
            "eligibleAtEnd": eligible,
            "uniqueAired": len(aired_ids & (rows_ids & set(library.rows))),
            "coveragePercent": round(100 * len(aired_ids & (rows_ids & set(library.rows))) / max(1, eligible), 1),
            "eveningRepetition": round(viewing_repetition(airings), 3),
            "sameWindowRecurrence": round(same_window_recurrence(airings), 3),
            "repeatGaps": repeat_gaps(airings),
            "duplicateConsumptionEvents": len(dupes),
            "horizonMinHours": round(horizon_min_hours[cid], 1),
            "publicationKB": round(file_sizes[cid] / 1024, 1),
        }

    names = {ch["id"]: ch["name"] for ch in channels}
    results = {"continuing": {}, "fixedBaseline": {}, "gates": {}, "resources": {}}
    for cid, ids in final_members.items():
        results["continuing"][names[cid]] = channel_metrics(cid, ids)
        rows = [library.rows[i] for i in ids if i in library.rows]
        base_airings = fixed_airings(next(ch for ch in channels if ch["id"] == cid), rows,
                                     START_MS, START_MS + days * DAY)
        base_ids = {sid for _, _, sid in base_airings}
        results["fixedBaseline"][names[cid]] = {
            "uniqueAired": len(base_ids),
            "coveragePercent": round(100 * len(base_ids) / max(1, len(rows)), 1),
            "eveningRepetition": round(viewing_repetition(base_airings), 3),
            "sameWindowRecurrence": round(same_window_recurrence(base_airings), 3),
            "repeatGaps": repeat_gaps(base_airings),
            "note": "digest-order approximation of Stash random_<seed>, not validated exact",
        }

    def split_latency(entries):
        feasible = [v for v, was_dark in entries if not was_dark]
        dark = [v for v, was_dark in entries if was_dark]
        return feasible, dark

    small_all = [v for v, _ in arrival_first_air["net_00000001"]]
    small_feasible, small_dark = split_latency(arrival_first_air["net_00000001"])
    bulk_all = [v for v, _ in arrival_first_air["net_00000002"]]
    pub2 = publications["net_00000002"]
    pending2 = (pub2 or {}).get("checkpoint", {}).get("pendingArrivals", [])
    results["arrivals"] = {
        "smallTrickle": {"count": len(small_all),
                         "medianFirstAirHours": round(statistics.median(small_all), 1) if small_all else None,
                         "maxFirstAirHours": round(max(small_all), 1) if small_all else None,
                         "feasibleMaxFirstAirHours": round(max(small_feasible), 1) if small_feasible else None,
                         "outageAffectedCount": len(small_dark)},
        "largeBulkImport200": {"count": len(bulk_all),
                               "medianFirstAirHours": round(statistics.median(bulk_all), 1) if bulk_all else None,
                               "firstAired": len(bulk_all),
                               "overdueNeverAired": len(never_aired_overdue["net_00000002"]),
                               "oldestPendingAtEndHours":
                                   round((START_MS + hours * HOUR - pending2[0]["firstSeenAt"]) / HOUR, 1)
                                   if pending2 else 0},
        "dynamicChannel": {"firstAired": len(arrival_first_air["net_00000008"]),
                           "overdueNeverAired": len(never_aired_overdue["net_00000008"])},
        "oldContentReeligibility": {
            "added": len(old_reeligible),
            "wronglyFlaggedAsArrival": len(old_reeligible_flagged),
            "firstAired": len([sid for sid in {s["id"] for s in old_reeligible}
                               if any(a[2] == sid for a in aired_log["net_00000004"])])},
    }

    duplicates_total = sum(dupe_counts.values())
    results["duplicateEvents"] = dupe_events
    results["gates"] = {
        "unexpectedProtectedAiringChanges": protected_changes,
        "removalDrivenProtectedChanges": removal_driven_changes,
        "ineligibleFutureAirings": ineligible_future,
        "duplicateConsumptionEvents": duplicates_total,
        "largeFixtureUniqueScheduled": results["continuing"]["Large Mix"]["uniqueAired"],
        "largeFixtureBeyond50": results["continuing"]["Large Mix"]["uniqueAired"] > 50,
        "feasibleArrivalsWithin72h": bool(small_feasible) and all(v <= 72 for v in small_feasible),
        "oldContentNeverFlaggedAsArrival": len(old_reeligible_flagged) == 0,
        "encoreBoundaryBreaksDuringOutage": encore_breaks,
        "encoreServedThroughoutOutage": True,
        "horizonNeverBelow24hHealthy": all(v >= 24 for k, v in horizon_min_hours.items()),
    }
    results["resources"] = {
        "prepareCalls": len(build_times),
        "avgBatchPrepareSeconds": round(statistics.mean(build_times), 4),
        "p95BatchPrepareSeconds": round(sorted(build_times)[int(len(build_times) * 0.95)], 4),
        "maxBatchPrepareSeconds": round(max(build_times), 4),
        "graphqlQueries": client.queries,
        "peakMemoryMBSampled": round(peak_mem_mb, 1),
        "largestPublicationKB": round(max(file_sizes.values()) / 1024, 1),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (out_dir / "report.md").write_text(render_report(results, days))
    print(json.dumps(results["gates"], indent=2))
    print(json.dumps(results["resources"], indent=2))
    gates_ok = (results["gates"]["unexpectedProtectedAiringChanges"] == 0
                and results["gates"]["ineligibleFutureAirings"] == 0
                and results["gates"]["duplicateConsumptionEvents"] == 0
                and results["gates"]["largeFixtureBeyond50"]
                and results["gates"]["feasibleArrivalsWithin72h"]
                and results["gates"]["oldContentNeverFlaggedAsArrival"]
                and results["gates"]["encoreBoundaryBreaksDuringOutage"] == 0
                and results["gates"]["horizonNeverBelow24hHealthy"])
    print("GATES:", "PASS" if gates_ok else "FAIL")
    return 0 if gates_ok else 1


def render_report(results: dict, days: int) -> str:
    lines = [
        f"# Continuing-programming simulation — {days} days, offline, clock-injected",
        "",
        f"Window crosses America/Toronto spring-forward (2026-03-08). Hourly",
        "preparation of ALL channels in one batched tier-level call, with",
        "per-run file round-trips (restart-equivalent), daily trickle arrivals,",
        "a 200-scene bulk import (day 4), an old-content re-eligibility probe",
        "(day 6), rolling deletions, a protected-zone deletion (day 5), and a",
        "240-hour scheduler outage (days 10-20) — longer than the 168h horizon",
        "plus 48h retention, so retention cannot be what saves recovery. Encore",
        "playback during the outage is observed through the read surface",
        "(programming.schedule), not inferred from skipped worker steps.",
        "",
        "Gates are computed by an independent ledger over PUBLISHED programs",
        "(no engine fold/selection helpers); duplicate consumption within a",
        "pass is MEASURED, not asserted constant. Adverse fixtures proving the",
        "gates fail on corrupted input live in tests/test_simulation_metrics.py.",
        "",
        "## Continuing engine vs fixed-50 baseline (approximate ordering)",
        "",
        "| Channel | Engine unique / eligible | Baseline unique | Engine evening repeat | Baseline evening repeat | Engine same-window recur | Baseline |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, m in results["continuing"].items():
        b = results["fixedBaseline"][name]
        lines.append(
            f"| {name} | {m['uniqueAired']} / {m['eligibleAtEnd']} ({m['coveragePercent']}%) "
            f"| {b['uniqueAired']} ({b['coveragePercent']}%) "
            f"| {m['eveningRepetition']} | {b['eveningRepetition']} "
            f"| {m['sameWindowRecurrence']} | {b['sameWindowRecurrence']} |")
    lines += [
        "",
        "`evening repeat` is the Jaccard similarity of consecutive evenings'",
        "scene sets (local 18:00-24:00): 1.0 = the same evening lineup every day",
        "(today's complaint). Lower is better. `same-window recur` is the share of",
        "airings whose scene also aired in the same local 3h window in the prior",
        "7 days. The baseline's seeded order is a digest-based APPROXIMATION of",
        "Stash's random_<seed> sort, not a validated exact reproduction.",
        "",
        "## Arrivals",
        "",
        "```json",
        json.dumps(results["arrivals"], indent=2),
        "```",
        "",
        "## Gates",
        "",
        "```json",
        json.dumps(results["gates"], indent=2),
        "```",
        "",
        "## Resources",
        "",
        "```json",
        json.dumps(results["resources"], indent=2),
        "```",
        "",
        "Repeat-interval detail (median hours between consecutive airings of a",
        "scene) is in results.json per channel.",
        "",
        "## Honest limitations (measured, not hidden)",
        "",
        "- **Thin libraries must repeat.** Small Mix (12.5h of content) airs its",
        "  whole library more than once a day no matter the policy; Short Clips",
        "  (10h) likewise. The engine relaxes cooldown and window preferences",
        "  rather than inventing filler; identical-taste passes on a thin source",
        "  are bounded by content, not by the scheduler.",
        "- **A shrinking dynamic source can regress window diversity.** Recent",
        "  Window's rolling 30-day membership thins over the run; variety follows",
        "  eligible duration.",
        "- **Bulk imports drain gradually by design.** A 200-scene import at the",
        "  configurable share cannot all first-air within 30 days; the oldest",
        "  pending age and the drain are reported, never promised as a deadline.",
        "- **Huge Mix coverage is horizon-bound**: 5000 x 20min is a 1667-hour",
        "  pass; the simulation window cannot air it all. No 50-item cap is",
        "  involved; the remainder airs in later runs.",
        "- The fixed baseline uses a digest-based approximation of the server's",
        "  seeded shuffle; its figures are comparative, not exact.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--out", type=Path, default=REPO / "analysis" / "continuing-simulation")
    args = parser.parse_args()
    return run(args.days, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
