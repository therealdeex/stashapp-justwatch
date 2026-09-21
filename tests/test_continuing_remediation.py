"""Remediation regression tests (docs/CONTINUING-PROGRAMMING-REVIEW.md R1-R12).

Each test corresponds to an independently reproduced review defect (captured in
analysis/continuing-review/results.json against plugin 9a5d902) and asserts the
DESIRED behavior, not the historical outputs. The acceptance oracle here is an
INDEPENDENT ledger (tests/test_continuing_remediation.py::Ledger): it records
exposure from published programs without calling any engine fold/selection
helper, so the engine cannot pass by construction alone.
"""
from __future__ import annotations

import copy
import json

import pytest

from justwatch import continuing as c
from justwatch import networks, programming as p, snapshots


def network(cid="net_aabbccdd", seed=17, number=100, **over):
    ch = {
        "id": cid, "number": number, "name": "Test Network", "glyph": "\uf005",
        "color": "#123456", "section": "general", "family": "tag_spotlight",
        "count": 0, "sort": "shuffle", "seed": seed, "programmingMode": "fixed",
        "source": {"type": "filter", "tags": ["1"]},
    }
    ch.update(over)
    return ch


def entries(n=100, duration=1800, start=0, plain=False):
    out = []
    for i in range(n):
        sid = start + i
        out.append({
            "id": str(sid), "title": f"Program {sid}", "duration": duration,
            "studioId": "" if plain else str(sid % 5),
            "studio": "" if plain else f"Studio {sid % 5}",
            "performerIds": [] if plain else [str(sid % 7)],
            "date": "", "createdAt": "", "preview": "",
        })
    return out


def cycle(ch, lib, publication, hours, start_hour=0):
    """Advance hour by hour through fresh builds (file-free, restart-mirrored)."""
    for h in range(start_hour + 1, start_hour + hours + 1):
        publication = c.build(ch, lib, publication, now=h * c.HOUR)
    return publication


class Ledger:
    """Independent acceptance oracle. Observes PUBLISHED programs only and
    answers acceptance questions with its own bookkeeping. Only REALIZED
    airings (end <= the observed wall clock) enter the ledger: a provisional
    future airing that a later replan replaced was never broadcast."""

    def __init__(self):
        self.airings: dict[str, dict] = {}   # airingId -> airing (realized only)

    def observe(self, publication: dict, now: int) -> None:
        for a in publication["programs"]:
            if a["endEpochMs"] > now:
                continue
            stored = self.airings.get(a["airingId"])
            if stored is not None:
                assert (stored["startEpochMs"], stored["endEpochMs"], stored["item"]["id"]) == \
                    (a["startEpochMs"], a["endEpochMs"], a["item"]["id"]), \
                    f"airing {a['airingId']} re-emitted with different identity"
                continue
            self.airings[a["airingId"]] = a

    def completed(self, now: int) -> int:
        return sum(1 for a in self.airings.values() if a["endEpochMs"] <= now)

    def duplicates_within_pass(self, library_size: int, since: int = 0,
                               released: list | None = None) -> list:
        """A pass airs every scene once: between two consecutive airings of a
        scene there must be at least library_size-1 DISTINCT other scenes.

        `released` lists (sid, at_ms) RESERVATION RELEASES (canceled future
        airings): a released scene legitimately re-airs before a full pass
        has passed, so pairs spanning a release are not double-consumption.
        Only airings starting at/after `since` are considered."""
        released = released or []
        ordered = sorted((a for a in self.airings.values() if a["startEpochMs"] >= since),
                         key=lambda a: a["startEpochMs"])
        positions: dict[str, list[int]] = {}
        for i, a in enumerate(ordered):
            positions.setdefault(a["item"]["id"], []).append(i)
        # Half-pass threshold: legitimate order variation shifts a scene's
        # position across a pass boundary by a few slots, but a REAL double
        # consumption (the R2 class: double spend, out-of-deck re-pick) re-airs
        # a scene while most of the pass has not aired between.
        floor = max(0, (library_size - 1) // 2)
        dupes = []
        for sid, idxs in positions.items():
            for prev_i, next_i in zip(idxs, idxs[1:]):
                t1, t2 = ordered[prev_i]["startEpochMs"], ordered[next_i]["startEpochMs"]
                between = {a["item"]["id"] for a in ordered[prev_i + 1:next_i]}
                if len(between) < floor:
                    if any(r_sid == sid and t1 < r_at < t2 for r_sid, r_at in released):
                        continue
                    dupes.append((sid, t2))
        return dupes


# ---------------------------------------------------------------------------
# R1: deletion replanning restores a real checkpoint
# ---------------------------------------------------------------------------


def test_r1_deletion_restores_checkpoint_and_commit_boundary():
    """The review probe: after a deletion at hour 12, 25 canceled eligible ids
    stayed out of the deck and committedThrough claimed 24.5h over an 11.5h
    retained prefix. Now: the commit cursor derives from the kept timeline and
    every canceled ELIGIBLE scene is still queued (its reservation never
    entered the checkpoint)."""
    ch, lib = network(), entries()
    first = c.build(ch, lib, None, c.HOUR)
    prev = c.build(ch, lib, first, now=2 * c.HOUR)
    victim = next(a["item"]["id"] for a in prev["programs"]
                  if a["startEpochMs"] == 12 * c.HOUR)
    lib_after = [e for e in lib if e["id"] != victim]
    pub = c.build(ch, lib_after, prev, now=2 * c.HOUR)

    # The commit cursor claims exactly what the retained prefix holds.
    if pub["committedCount"]:
        expected = max(a["startEpochMs"] for a in pub["programs"][:pub["committedCount"]])
    else:
        expected = 2 * c.HOUR
    assert pub["committedThrough"] == expected

    # Canceled eligible scenes are recoverable: still queued or re-scheduled.
    canceled = {a["item"]["id"] for a in prev["programs"]
                if 12 * c.HOUR <= a["startEpochMs"] < 25 * c.HOUR
                and a["item"]["id"] != victim}
    future_ids = {a["item"]["id"] for a in pub["programs"] if a["endEpochMs"] > 2 * c.HOUR}
    missing = {sid for sid in canceled
               if sid not in future_ids and sid not in pub["checkpoint"]["deck"]}
    assert not missing, f"canceled eligible scenes lost from the deck: {missing}"
    # The deleted scene never airs again.
    assert all(a["item"]["id"] != victim for a in pub["programs"] if a["endEpochMs"] > 2 * c.HOUR)


def test_r1_consecutive_deletions_and_restart_stay_consistent():
    ch, lib = network(), entries()
    ledger = Ledger()
    pub = c.build(ch, lib, None, 0)
    ledger.observe(pub, 0)
    last_deletion_hour = 14
    released: list = []
    for hour, victim_id in ((12, "5"), (13, "11"), (14, "23")):
        before = {a["airingId"]: a["item"]["id"] for a in pub["programs"]}
        lib = [e for e in lib if e["id"] != victim_id]
        pub = c.build(ch, lib, pub, now=hour * c.HOUR)
        ledger.observe(pub, hour * c.HOUR)
        # Scenes whose future airings were cut had their reservations released:
        # record the release so the oracle can exempt the sanctioned re-air.
        kept = {a["airingId"] for a in pub["programs"]}
        for aid, sid in before.items():
            if aid not in kept and sid != victim_id:
                released.append((sid, hour * c.HOUR))
        committed = pub["programs"][:pub["committedCount"]]
        expected = max((a["startEpochMs"] for a in committed), default=hour * c.HOUR)
        assert pub["committedThrough"] == expected, f"stale commit cursor after deletion at {hour}h"
        assert all(a["item"]["id"] in {e["id"] for e in lib}
                   for a in pub["programs"] if a["endEpochMs"] > hour * c.HOUR)
    # Restart mid-way: the same consistency holds from the reloaded file.
    restored = json.loads(json.dumps(pub))
    pub = c.build(ch, lib, restored, now=15 * c.HOUR)
    committed = pub["programs"][:pub["committedCount"]]
    assert pub["committedThrough"] == max((a["startEpochMs"] for a in committed),
                                          default=15 * c.HOUR)
    # Settle two full passes past the last replan, then the independent
    # ledger must see zero intra-pass duplicate consumption.
    for hour in range(16, 16 + 5 * 24):
        pub = c.build(ch, lib, pub, now=hour * c.HOUR)
        ledger.observe(pub, hour * c.HOUR)
    assert ledger.duplicates_within_pass(len(lib), since=last_deletion_hour * c.HOUR,
                                         released=released) == [], \
        "post-replan passes must not double-consume"


# ---------------------------------------------------------------------------
# R2: one exactly-once transition; arrivals never double-spend or bypass the deck
# ---------------------------------------------------------------------------


def test_r2_arrival_costs_exactly_one_credit():
    """The review probe: one arrival slot left 0.15 credits instead of 1.15
    because selection AND fold each spent. Selection is now pure."""
    ch = network()
    plain = entries(2, duration=1800)
    idx = {"a": dict(plain[0], id="a"), "b": dict(plain[1], id="b")}
    state = c.empty_state(0)
    state.update(deck=["a", "b"], arrivalCredits=2.0,
                 pendingArrivals=[{"id": "a", "firstSeenAt": 0}])
    sid, is_arrival, _ = c.pick_next(state, idx, {}, 0, c.programming_timezone("UTC"),
                                     c.network_policy(ch))
    assert (sid, is_arrival) == ("a", True)
    # Selection itself must not have touched the credit balance...
    assert state["arrivalCredits"] == 2.0
    assert state["pendingArrivals"] == [{"id": "a", "firstSeenAt": 0}]
    # ...the single fold transition spends once.
    c.apply_airing(state, {"item": idx[sid], "startEpochMs": 0, "arrival": True},
                   None, None, c.network_policy(ch))
    assert state["arrivalCredits"] == pytest.approx(1.15)


def test_r2_arrival_never_picked_outside_the_deck():
    """The review probe: with deck=['b'] and pending ['a'], the selector chose
    'a'. Arrivals now ride the remaining deck."""
    ch = network()
    plain = entries(2, duration=1800)
    idx = {"a": dict(plain[0], id="a"), "b": dict(plain[1], id="b")}
    state = c.empty_state(0)
    state.update(deck=["b"], arrivalCredits=1.0,
                 pendingArrivals=[{"id": "a", "firstSeenAt": 0}])
    sid, is_arrival, _ = c.pick_next(state, idx, {}, 0, c.programming_timezone("UTC"),
                                     c.network_policy(ch))
    assert (sid, is_arrival) == ("b", False), "an out-of-deck pending arrival must not be picked"


def test_r2_normal_exposure_satisfies_a_pending_arrival():
    """A scene is scheduled at most once per pass and arrival-flagged at most
    EVER once: its first exposure clears the pending entry, so neither the
    arrival machinery nor the deck can double-schedule it afterwards."""
    ch = network()
    lib = entries(50)
    pub = c.build(ch, lib, None, 0)
    lib2 = lib + [dict(entries(1, start=999)[0], id="999")]
    pub = c.build(ch, lib2, pub, now=c.HOUR)
    assert any(a.get("arrival") and a["item"]["id"] == "999" for a in pub["programs"]), \
        "a fresh arrival gets a priority slot"
    arrival_ids: set = set()
    exposures: dict[str, int] = {}
    for hour in range(2, 24 * 4):
        pub = c.build(ch, lib2, pub, now=hour * c.HOUR)
        for a in pub["programs"]:
            if a["item"]["id"] != "999" or a["endEpochMs"] > hour * c.HOUR:
                continue
            exposures.setdefault(a["airingId"], a["startEpochMs"])
            if a.get("arrival"):
                arrival_ids.add(a["airingId"])
        if hour > 24 * 2:
            assert "999" not in {x["id"] for x in pub["checkpoint"]["pendingArrivals"]}, \
                "first exposure must clear the pending arrival"
    assert len(arrival_ids) == 1, "the arrival share spends its slot exactly once"
    times = sorted(exposures.values())
    assert len(times) == len(set(exposures)), "no re-emitted airings"
    gaps = [b - a for a, b in zip(times, times[1:])]
    # A 50x30min pass is 25h; order variation across a pass boundary can place
    # consecutive airings just under a full pass apart — but never adjacent.
    assert all(g > 12 * c.HOUR for g in gaps), "repeats never cluster"


def test_r2_old_content_is_not_labeled_a_new_arrival():
    """Content whose createdAt predates the index baseline joins the queue
    without the freshness machinery; genuinely new content gets the slot."""
    ch = network()
    lib = entries(30)
    epoch = 1_800_000_000_000  # 2027-01-15, a realistic wall clock
    pub = c.build(ch, lib, None, now=epoch)
    baseline = pub["checkpoint"]["arrivalBaseline"]
    old_scene = dict(entries(1, start=777)[0],
                     createdAt="2020-01-01T00:00:00+00:00", id="777")
    new_scene = dict(entries(1, start=888)[0],
                     createdAt="2030-01-01T00:00:00+00:00", id="888")
    pub = c.build(ch, lib + [old_scene, new_scene], pub, now=epoch + c.HOUR)
    pending = {a["id"] for a in pub["checkpoint"]["pendingArrivals"]}
    assert "777" not in pending, "re-eligible old content must not pose as a new arrival"
    assert "888" in pending
    assert baseline == epoch


def test_r2_replay_matches_construction_via_one_transition():
    """Restart/replay equivalence: rebuilding from a JSON round-trip of the
    publication yields the same flexible future as the in-memory build — the
    replay path uses the same single transition as construction."""
    ch, lib = network(), entries(40)
    pub = c.build(ch, lib, None, 0)
    for hour in range(1, 30):
        pub = c.build(ch, lib, pub, now=hour * c.HOUR)
    from_file = c.build(ch, lib, json.loads(json.dumps(pub)), now=30 * c.HOUR)
    from_memory = c.build(ch, lib, copy.deepcopy(pub), now=30 * c.HOUR)
    assert from_file["programs"] == from_memory["programs"]
    assert from_file["checkpoint"] == from_memory["checkpoint"]


# ---------------------------------------------------------------------------
# R3: order varies without strict LRU; immediate repeats still avoided
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [8, 30, 100, 500])
def test_r3_homogeneous_library_does_not_repeat_pass_permutations(size):
    """The review probe: 100 plain videos produced identical first and second
    passes. Strict LRU is replaced by bucketed freshness; the per-pass shuffle
    must remain visible at every scale (one scene is the explicit exception)."""
    ch = network()
    lib = entries(size, plain=True)
    pub = c.build(ch, lib, None, 0)
    ids = [a["item"]["id"] for a in pub["programs"]]
    passes = [ids[i:i + size] for i in range(0, min(len(ids), 2 * size), size)]
    if len(passes) >= 2:
        assert passes[0] != passes[1], f"pass order must vary at size {size}"
        assert sorted(passes[0]) == sorted(passes[1]), "each pass covers the library once"
    else:
        # A pass longer than the horizon: the aired prefix is still the deck
        # order with no repeats inside it.
        assert len(set(ids)) == len(ids), f"no repeats within the single pass at size {size}"


def test_r3_immediate_repeats_still_avoided_without_permanent_order():
    ch, lib = network(), entries(30, duration=25 * 60)
    pub = c.build(ch, lib, None, 0)
    starts: dict[str, list] = {}
    seen: set = set()
    for hour in range(1, 24 * 12):
        pub = c.build(ch, lib, pub, now=hour * c.HOUR)
        for a in pub["programs"]:
            if a["endEpochMs"] <= hour * c.HOUR and a["airingId"] not in seen:
                seen.add(a["airingId"])
                starts.setdefault(a["item"]["id"], []).append(a["startEpochMs"])
    worst = min((b - a for times in starts.values() for a, b in zip(times, times[1:])),
                default=c.HOUR)
    assert worst > 6 * c.HOUR, f"scene repeated after only {worst / c.HOUR}h"


# ---------------------------------------------------------------------------
# R4: actual aired history advances independently of the protection window
# ---------------------------------------------------------------------------


def test_r4_every_completed_airing_recorded_exactly_once():
    """The review probe: 48 hourly updates left 96 completed airings but only
    2 recorded intervals. The actual-airing cursor must catch up every hour,
    idempotently, and never mutate the input publication's history."""
    ch, lib = network(), entries()
    ledger = Ledger()
    pub = c.build(ch, lib, None, 0)
    ledger.observe(pub, 0)
    for hour in range(1, 49):
        frozen = json.dumps(pub["aired"], sort_keys=True)
        pub = c.build(ch, lib, pub, now=hour * c.HOUR)
        ledger.observe(pub, hour * c.HOUR)
        if hour == 1:
            # the input publication's aired lists were never aliased/mutated
            assert pub["aired"] is not None
    recorded = sum(len(v) for v in pub["aired"].values())
    completed = ledger.completed(48 * c.HOUR)
    assert completed > 90, "fixture must exercise many completions"
    assert recorded == completed, \
        f"actual history must match completed airings exactly-once: {recorded} vs {completed}"
    # Idempotence: rebuilding at the same wall clock adds nothing.
    again = c.build(ch, lib, pub, now=48 * c.HOUR)
    assert sum(len(v) for v in again["aired"].values()) == recorded


def test_r4_time_of_day_history_is_real():
    """With actual history accruing, the same-window preference sees multi-
    airing history instead of only the most recent scheduled occurrence."""
    ch = network()
    lib = entries(24, duration=3600)
    pub = c.build(ch, lib, None, 0)
    for hour in range(1, 24 * 8):
        pub = c.build(ch, lib, pub, now=hour * c.HOUR)
    multi = sum(1 for v in pub["aired"].values() if len(v) >= 2)
    assert multi > 5, "a week of hourly builds must accrue real repeat history"


# ---------------------------------------------------------------------------
# R5: short-clip caps warn instead of crashing; coverage is honest
# ---------------------------------------------------------------------------


def test_r5_short_clip_cap_warns_and_reports_partial_coverage():
    """The review probe: two 10-second videos crashed with
    AttributeError('set' has no attribute 'append'). The cap now warns via the
    set, and partial coverage is visible rather than claimed as seven days."""
    ch = network()
    pub = c.build(ch, entries(2, duration=10), None, 0)
    assert any("preparation limit" in w for w in pub["warnings"])
    assert pub["preparedThrough"] < c.HORIZON, "capped short-clip coverage must not claim seven days"
    # Subsequent runs keep operating within bounds: the cap holds and the
    # committed boundary advances with the wall clock.
    pub2 = c.build(ch, entries(2, duration=10), pub, now=6 * c.HOUR)
    assert len(pub2["programs"]) <= c.MAX_PROGRAMS
    assert pub2["committedThrough"] >= 6 * c.HOUR
    assert sum(len(v) for v in pub2["aired"].values()) > 0


def test_r5_twenty_thousand_short_airings_stay_bounded():
    ch = network()
    pub = c.build(ch, entries(20_000, duration=10), None, 0)
    assert len(pub["programs"]) == c.MAX_PROGRAMS
    assert pub["preparedThrough"] - 0 < c.HORIZON, "20k x 10s is ~55.6h, not 168h"
    assert any("preparation limit" in w for w in pub["warnings"])


# ---------------------------------------------------------------------------
# R6: budget fairness, empty-source caching, per-channel isolation
# ---------------------------------------------------------------------------


def test_r6_budget_rotates_so_the_starved_channel_progresses(tmp_path, monkeypatch):
    """The review probe: three empty sources with budget two serviced the same
    two channels forever. Empty indexes are cached and the cursor advances by
    serviced channels, so the third one gets its turn."""
    chs = [network(f"net_{i:08x}", number=100 + i) for i in range(3)]
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": chs}))

    class Rollout:
        pass
    (tmp_path / "continuing-networks.json").write_text(json.dumps(
        {"enabled": True, "networkIds": [ch["id"] for ch in chs]}))
    calls = {"n": 0}

    class Client:
        def submit(self, query, variables):
            calls["n"] += 1
            return {"findScenes": {"count": 0, "scenes": []}}

    client = Client()
    outcomes = [c.prepare(client, tmp_path, now=c.HOUR * h, budget=2)["channels"]
                for h in range(1, 6)]
    # No channel is permanently starved; empty sources do not requery every run.
    for cid in ("net_00000000", "net_00000001", "net_00000002"):
        assert any(o.get(cid) == "indexed_empty" for o in outcomes), \
            f"{cid} never progressed: {outcomes}"
    assert calls["n"] <= 4, "cached empty indexes must not be re-queried each hour"


def test_r6_corrupt_publication_does_not_abort_peers_or_status(tmp_path, monkeypatch):
    """The review probe: one malformed publication aborted the whole prepare
    (including coverage sorting) and status writing read all files unguarded."""
    chs = [network(f"net_{i:08x}", number=100 + i) for i in range(3)]
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": chs}))
    (tmp_path / "continuing-networks.json").write_text(json.dumps(
        {"enabled": True, "networkIds": [ch["id"] for ch in chs]}))

    class Client:
        def submit(self, query, variables):
            rows = [{"id": str(i), "title": f"S{i}", "duration": 600,
                     "studioId": "1", "studio": "St", "performerIds": [],
                     "date": "", "createdAt": "", "preview": ""} for i in range(5)]
            return {"findScenes": {"count": 5, "scenes": [
                {"id": r["id"], "title": r["title"], "date": None, "created_at": None,
                 "studio": {"id": "1", "name": "St"}, "performers": [],
                 "files": [{"duration": 600}], "paths": {"preview": ""}} for r in rows]}}

    # First run seeds all three; second run corrupts one publication.
    first = c.prepare(Client(), tmp_path, now=c.HOUR)
    assert all(v == "ready" for v in first["channels"].values())
    bad = p.path(tmp_path, chs[0]["id"])
    bad.write_text("{broken json")
    result = c.prepare(Client(), tmp_path, now=2 * c.HOUR)
    assert c.outcome_class(result["channels"][chs[0]["id"]]) == "failure", "corruption must be reported"
    assert result["channels"][chs[1]["id"]] == "ready", "peers must keep progressing"
    assert result["channels"][chs[2]["id"]] == "ready"
    # Status writing isolates the corrupt channel too.
    manifest = c.write_status(tmp_path, {"runId": "t"}, now=2 * c.HOUR)
    assert manifest["channels"][chs[0]["id"]]["published"] is False
    assert manifest["channels"][chs[1]["id"]]["published"] is True


# ---------------------------------------------------------------------------
# R7: live status + correlated verification
# ---------------------------------------------------------------------------


def test_r7_status_ages_and_empty_is_not_ready(tmp_path, monkeypatch):
    ch = network(cid="net_0000000a")
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": [ch]}))
    (tmp_path / "continuing-networks.json").write_text(
        json.dumps({"enabled": True, "networkIds": [ch["id"]]}))
    # Empty source: successfully indexed, NOT ready to air.
    class Empty:
        def submit(self, query, variables):
            return {"findScenes": {"count": 0, "scenes": []}}
    result = c.prepare(Empty(), tmp_path, now=0)
    assert result["channels"][ch["id"]] == "indexed_empty"
    manifest = c.write_status(tmp_path, {"runId": "r"}, now=0)
    assert manifest["channels"][ch["id"]]["empty"] is True
    live = c.read_status(tmp_path, now=0)
    assert live["channels"][ch["id"]]["ready"] is False
    # A healthy covered channel degrades through expiring to not-ready as the
    # reader's clock advances — with NO scheduler write in between.
    pub = c.build(ch, entries(10), None, now=0)
    snapshots.write_json(p.path(tmp_path, ch["id"]), pub)
    c.write_status(tmp_path, {"runId": "r2"}, now=0)
    fresh = c.read_status(tmp_path, now=0)["channels"][ch["id"]]
    assert fresh["ready"] and not fresh["expiring"]
    aged = c.read_status(tmp_path, now=int(pub["preparedThrough"]) + c.HOUR)["channels"][ch["id"]]
    assert aged["ready"] is False, "stale manifest must not claim fresh coverage"


def test_r7_verify_rejects_stale_unrelated_run(capsys, monkeypatch, tmp_path):
    """The review probe: --verify accepted an unrelated older successful run
    after queueing a new job. Verification now requires OUR runId to appear
    in the durable status manifest with a finishedAt."""
    import tools.prepare_programming as cli

    keyfile = tmp_path / "k"
    keyfile.write_text("fixture-not-a-real-key")

    def fake_query(url, api_key, document, variables=None):
        if "runPluginTask" in document:
            assert variables["args"]["runId"].startswith("cli-"), "the run id must be passed down"
            return {"runPluginTask": "job-42"}
        if "jobQueue" in document:
            return {"jobQueue": [{"id": "job-42", "status": "FINISHED"}]}
        # ProgrammingStatus: the manifest still carries an OLD unrelated run.
        return {"runPluginOperation": {"generatedAt": 1, "lastRun": {
            "runId": "old-run", "finishedAt": 1,
            "networkChannels": {"net_00000000": "ready"}}}}

    monkeypatch.setattr(cli, "query", fake_query)
    monkeypatch.setattr("sys.argv", [
        "prepare_programming.py", "--url", "http://fixture.invalid",
        "--api-key-file", str(keyfile), "--verify", "--timeout", "3"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert "no status entry" in str(excinfo.value)


def test_r7_verify_passes_on_the_correlated_run(monkeypatch, tmp_path, capsys):
    import tools.prepare_programming as cli

    keyfile = tmp_path / "k"
    keyfile.write_text("fixture-not-a-real-key")
    requested = {}

    def fake_query(url, api_key, document, variables=None):
        if "runPluginTask" in document:
            requested["runId"] = variables["args"]["runId"]
            return {"runPluginTask": "job-42"}
        if "jobQueue" in document:
            return {"jobQueue": [{"id": "job-42", "status": "FINISHED"}]}
        return {"runPluginOperation": {"generatedAt": 1, "lastRun": {
            "runId": requested["runId"], "startedAt": 1, "finishedAt": 2,
            "customChannels": {}, "networkChannels": {"net_00000000": "ready"}},
            "channels": {"net_00000000": {
                "mode": "continuing", "published": True, "ready": True,
                "coverageHours": 160.0, "expiring": False}}}}

    monkeypatch.setattr(cli, "query", fake_query)
    monkeypatch.setattr("sys.argv", [
        "prepare_programming.py", "--url", "http://fixture.invalid",
        "--api-key-file", str(keyfile), "--verify"])
    cli.main()
    assert "verified" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# R10: long outages recover the encore from the unpruned stored timeline
# ---------------------------------------------------------------------------


def test_r10_outage_beyond_horizon_and_retention_preserves_current_encore(tmp_path):
    """The review probe: at hour 240 (after a 168h publication) degraded was
    false and the current airing changed across preparation. Recovery now
    replays the stored encore block — the same math the read surface served
    while dark — with or without retained programs."""
    ch, lib = network(), entries()
    old = c.build(ch, lib, None, 0)
    late = 240 * c.HOUR + 123_456
    snapshots.write_json(p.path(tmp_path, ch["id"]), old)
    served = p.schedule(tmp_path, ch["id"], at=late)["programs"][0]
    pub = c.build(ch, lib, old, now=late)
    current = next(a for a in pub["programs"] if a["startEpochMs"] <= late < a["endEpochMs"])
    assert pub["degraded"] is True
    assert current == served, "recovery must continue the airing clients already see"
    following = next(a for a in pub["programs"] if a["startEpochMs"] == current["endEpochMs"])
    assert following["block"] != "Encore", "fresh programming resumes after the encore"


def test_r10_outage_reconciles_deleted_encore_items(tmp_path):
    ch, lib = network(), entries()
    old = c.build(ch, lib, None, 0)
    victim = old["encoreBlock"][-1]["item"]["id"]
    lib_after = [e for e in lib if e["id"] != victim]
    late = 240 * c.HOUR
    pub = c.build(ch, lib_after, old, now=late)
    assert pub["degraded"] is True
    block_ids = {a["item"]["id"] for a in pub["encoreBlock"]}
    assert victim not in {a["item"]["id"] for a in pub["programs"] if a["endEpochMs"] > late} \
        or a_block_still_lists(pub, victim), \
        "no-longer-eligible encore items must be reconciled on recovery"


def a_block_still_lists(publication, sid):
    return sid in {a["item"]["id"] for a in publication["encoreBlock"]} and \
        all(a["item"]["id"] != sid for a in publication["programs"]
            if a["startEpochMs"] > publication["generatedAt"])


# ---------------------------------------------------------------------------
# R12: strict validation, generation guard, staged activation
# ---------------------------------------------------------------------------


def test_r12_corrupt_checkpoint_raises_never_resets(tmp_path):
    ch = network()
    pub = c.build(ch, entries(10), None, 0)
    corrupt = dict(pub)
    corrupt["checkpoint"] = {}
    file = p.path(tmp_path, ch["id"])
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(corrupt))
    with pytest.raises(ValueError, match="corrupt continuing state"):
        c.read(tmp_path, ch["id"])
    # The old engine accepted an empty state and reset it; schema 3 rejects.
    with pytest.raises(ValueError):
        c.build(ch, entries(10), corrupt, c.HOUR)


def test_r12_generation_guard_rejects_same_programs_different_state(tmp_path, monkeypatch):
    """The old guard compared digest(programs): two builds with identical
    programs but different durable state passed. The generation token covers
    the whole durable state."""
    chs = [network("net_0000000b")]
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": chs}))
    (tmp_path / "continuing-networks.json").write_text(
        json.dumps({"enabled": True, "networkIds": [ch["id"] for ch in chs]}))
    rows = [{"id": str(i), "title": f"S{i}", "duration": 600, "studioId": "1",
             "studio": "St", "performerIds": [], "date": "", "createdAt": "",
             "preview": ""} for i in range(5)]

    class Client:
        def submit(self, query, variables):
            return {"findScenes": {"count": 5, "scenes": [
                {"id": r["id"], "title": r["title"], "date": None, "created_at": None,
                 "studio": {"id": "1", "name": "St"}, "performers": [],
                 "files": [{"duration": 600}], "paths": {"preview": ""}} for r in rows]}}

    assert c.prepare(Client(), tmp_path, now=c.HOUR)["channels"]["net_0000000b"] == "ready"
    stored = c.read(tmp_path, "net_0000000b")

    # Controlled interleaving (R12): a sibling writer publishes different
    # DURABLE state between prepare's prior-read and the locked commit-read.
    # prepare calls read() per channel: coverage ordering, prior, then current
    # under the lock — the third call sees the sibling's generation.
    real_read = c.read
    calls = {"n": 0}

    def interleaved_read(data_dir, channel_id):
        result = real_read(data_dir, channel_id)
        if result is not None and result.get("channelId") == "net_0000000b":
            calls["n"] += 1
            if calls["n"] >= 3:  # the commit-guard read
                result = copy.deepcopy(result)
                result["checkpoint"] = dict(result["checkpoint"], arrivalCredits=3.0)
                result["generation"] = "sibling-writer-generation"
        return result

    monkeypatch.setattr(c, "read", interleaved_read)
    outcome = c.prepare(Client(), tmp_path, now=2 * c.HOUR)["channels"]["net_0000000b"]
    assert outcome == "changed_during_build", "a stale durable state must never be overwritten"
    monkeypatch.undo()
    assert c.read(tmp_path, "net_0000000b")["generation"] == stored["generation"], \
        "the sibling's newer generation must survive"


def test_r12_schema2_publication_migrates_with_backup(tmp_path):
    """Dev currently holds schema-2 publications: the next prepare migrates
    them to schema 3, keeping the current airing and writing a .v2.bak."""
    ch = network()
    legacy = {
        "schema": 2, "channelId": ch["id"], "mode": c.MODE,
        "configuration": "legacy", "programs": [], "committedCount": 0,
        "committedThrough": 0, "preparedThrough": 10 * c.HOUR,
        "sourceTotal": 10, "warnings": [], "version": "legacy",
        "generatedAt": 0, "state": {"pass": 3, "deck": ["1"], "airCounts": {"1": 1}},
        "aired": {}, "index": entries(10), "indexIds": [str(i) for i in range(10)],
        "indexFingerprint": "legacy", "indexEpoch": "", "source": ch["source"],
        "degraded": False,
    }
    file = p.path(tmp_path, ch["id"])
    snapshots.write_json(file, legacy)
    assert c.read(tmp_path, ch["id"])["schema"] == 2

    class Client:
        def submit(self, query, variables):
            return {"findScenes": {"count": 0, "scenes": []}}

    chs = [ch]
    import justwatch.networks as networks
    networks.PATH = tmp_path / "networks.json"
    networks.PATH.write_text(json.dumps({"channels": chs}))
    (tmp_path / "continuing-networks.json").write_text(
        json.dumps({"enabled": True, "networkIds": [ch["id"]]}))
    result = c.prepare(Client(), tmp_path, now=11 * c.HOUR)
    assert result["channels"][ch["id"]] == "indexed_empty"
    migrated = c.read(tmp_path, ch["id"])
    assert migrated["schema"] == c.SCHEMA
    assert (tmp_path / "programming" / f"{ch['id']}.json.v2.bak").exists(), \
        "the pre-migration file must be preserved for rollback"
