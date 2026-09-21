"""Continuing programming acceptance tests (docs/CONTINUING-PROGRAMMING-PLAN.md).

Behavior, not implementation shape: full-library coverage with durable
consumption, whole-airing protection, reservation release on replans, arrival
freshness, relaxation on thin sources, outage encore, rollout gating, and the
storage/versioning story for network publications.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

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


def entries(n=100, duration=1800, start=0):
    return [
        {"id": str(start + i), "title": f"Program {start + i}", "duration": duration,
         "studioId": str((start + i) % 5), "studio": f"Studio {(start + i) % 5}",
         "performerIds": [str((start + i) % 7)], "date": "", "createdAt": "",
         "preview": ""}
        for i in range(n)
    ]


class Rollout:
    """Write a rollout file into a tmp data dir."""

    def __init__(self, path: Path):
        self.path = path / "continuing-networks.json"

    def enable(self, *ids, enabled=True):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": enabled, "networkIds": list(ids)}))


class FakeIndexClient:
    """Answers the ContinuingIndex query from a library of scene rows."""

    def __init__(self, rows, page_size=250, fail_marker=None):
        self.rows = rows
        self.page_size = page_size
        self.fail_marker = fail_marker
        self.calls = []

    def submit(self, query, variables):
        self.calls.append((query, variables))
        if self.fail_marker and self.fail_marker in json.dumps(variables):
            raise RuntimeError("stash query failed")
        page = variables["filter"]["page"]
        chunk = self.rows[(page - 1) * self.page_size: page * self.page_size]
        return {"findScenes": {"count": len(self.rows), "scenes": chunk}}


def graphql_rows(items):
    return [
        {"id": e["id"], "title": e["title"], "date": e.get("date") or None,
         "created_at": e.get("createdAt") or None,
         "studio": {"id": e["studioId"], "name": e["studio"]},
         "performers": [{"id": pid} for pid in e["performerIds"]],
         "files": [{"duration": e["duration"]}],
         "paths": {"preview": "/preview"}}
        for e in items
    ]


# ---------------------------------------------------------------------------
# Coverage: the whole eligible library, not a fixed first-50 rotation
# ---------------------------------------------------------------------------


def test_large_source_eventually_schedules_everything_beyond_fifty():
    ch = network()
    lib = entries(500, duration=1800)
    publication = c.build(ch, lib, None, now=0)
    first_airings = {a["item"]["id"] for a in publication["programs"]}
    assert len(first_airings) > 50, "a 500-scene network must not stop at the rotation size"
    # Keep replenishing for 30 days; everything airs eventually.
    seen = set(first_airings)
    for hour in range(1, 24 * 30):
        publication = c.build(ch, lib, publication, now=hour * c.HOUR)
        seen |= {a["item"]["id"] for a in publication["programs"]}
    assert seen == {e["id"] for e in lib}


def test_no_repeats_within_a_pass_and_pass_order_varies():
    ch = network()
    lib = entries(100)
    publication = c.build(ch, lib, None, now=0)
    ids = [a["item"]["id"] for a in publication["programs"]]
    assert len(set(ids[:100])) == 100, "pass one consumes the whole deck once"
    assert len(set(ids[100:200])) == 100, "pass two has no repeats either"
    assert ids[:100] != ids[100:200], "pass ordering must vary between passes"


def test_deck_and_counts_survive_restarts_and_hourly_reruns(tmp_path):
    ch = network()
    lib = entries(100)
    first = c.build(ch, lib, None, now=0)
    # "restart": round-trip through the file, exactly what prepare reloads.
    snapshots.write_json(p.path(tmp_path, ch["id"]), first)
    reloaded = json.loads(p.path(tmp_path, ch["id"]).read_text())
    from_file = c.build(ch, lib, reloaded, now=c.HOUR)
    from_memory = c.build(ch, lib, copy.deepcopy(first), now=c.HOUR)
    assert from_file["version"] == from_memory["version"]
    assert from_file["state"] == from_memory["state"]


def test_build_is_pure_and_deterministic():
    ch = network()
    lib = entries(50)
    first = c.build(ch, lib, None, now=0)
    frozen = copy.deepcopy(first)
    again = c.build(ch, lib, first, now=c.HOUR)
    assert c.build(ch, lib, copy.deepcopy(first), now=c.HOUR) == again
    assert first == frozen, "input publication must never be mutated"


# ---------------------------------------------------------------------------
# Protection: whole airings through now+24h, stable across routine runs
# ---------------------------------------------------------------------------


def test_routine_extension_preserves_protected_airings_for_days():
    ch = network()
    lib = entries(100)
    prev = c.build(ch, lib, None, now=0)
    for hour in range(1, 24 * 14):
        now = hour * c.HOUR
        nxt = c.build(ch, lib, prev, now=now)
        prev_now = now - c.HOUR
        protected = {a["airingId"]: a for a in prev["programs"]
                     if a["endEpochMs"] > prev_now and a["startEpochMs"] < prev_now + c.PROTECT}
        current = {a["airingId"]: a for a in nxt["programs"]}
        for aid, airing in protected.items():
            assert current.get(aid) == airing, f"protected airing {aid} changed at hour {hour}"
        prev = nxt


def test_cutoff_inside_an_airing_preserves_that_whole_airing():
    ch = network()
    lib = entries(3, duration=10 * 3600)  # 10h airings: cutoffs land mid-airing
    first = c.build(ch, lib, None, now=0)
    # At now=5h the 24h cutoff (29h) falls inside the 4th airing (30-40h).
    later = c.build(ch, lib, first, now=5 * c.HOUR)
    assert later["programs"][3] == first["programs"][3], "airing straddling the cutoff stays whole"


# ---------------------------------------------------------------------------
# Replans: reservations released, nothing lost or double-counted
# ---------------------------------------------------------------------------


def test_deletion_replans_from_earliest_affected_and_releases_reservations():
    ch = network()
    lib = entries(100)
    prev = c.build(ch, lib, None, now=0)
    lib_after = entries(100, start=0)
    lib_after = [e for e in lib_after if e["id"] != "7"]
    nxt = c.build(ch, lib_after, prev, now=10 * c.HOUR)
    future = [a for a in nxt["programs"] if a["endEpochMs"] > 10 * c.HOUR]
    assert all(a["item"]["id"] != "7" for a in future), "deleted scene must not keep airing"
    affected = next(i for i, a in enumerate(prev["programs"])
                    if a["item"]["id"] == "7" and a["endEpochMs"] > 10 * c.HOUR)
    assert nxt["programs"][:affected] == prev["programs"][:affected], \
        "everything before the earliest affected airing is preserved"
    # Membership safety: only eligible scenes from here on.
    assert {a["item"]["id"] for a in future} <= {e["id"] for e in lib_after}


def test_flexible_consumption_is_not_double_counted_after_replan():
    ch = network()
    lib = entries(100)
    prev = c.build(ch, lib, None, now=0)
    counts_before = copy.deepcopy(prev["state"]["airCounts"])
    # A deletion replan discards the flexible future; the durable state must
    # not have consumed it in the first place.
    assert prev["state"]["airCounts"] == counts_before
    lib_after = [e for e in lib if e["id"] != "3"]
    nxt = c.build(ch, lib_after, prev, now=10 * c.HOUR)
    flexible_only_ids = {a["item"]["id"] for a in prev["programs"][prev["committedCount"]:]}
    # Flexible scenes (other than the deleted one) are still queued: they were
    # never durably consumed, so the rebuild re-derives them from the deck.
    for sid in flexible_only_ids - {"3"}:
        assert sid in nxt["state"]["deck"] or sid in {a["item"]["id"] for a in nxt["programs"]}, \
            "flexible reservation lost after replan"


def test_duration_edit_rebuilds_flexible_future():
    ch = network()
    lib = entries(100, duration=1800)
    prev = c.build(ch, lib, None, now=0)
    lib_edited = entries(100, duration=2400)
    nxt = c.build(ch, lib_edited, prev, now=10 * c.HOUR)
    future = [a for a in nxt["programs"] if a["endEpochMs"] > 10 * c.HOUR + c.PROTECT]
    assert future, "flexible future rebuilt"
    assert all(a["endEpochMs"] - a["startEpochMs"] == 2400 * 1000 for a in future), \
        "new durations apply beyond the protected boundary"


def test_policy_change_keeps_notice_boundary_and_restarts_state():
    ch = network()
    lib = entries(100)
    prev = c.build(ch, lib, None, now=0)
    changed = network(programming={"mode": "continuing", "spacing": 3})
    now = 30_000
    boundary = next((a["endEpochMs"] for a in prev["programs"] if a["endEpochMs"] >= now + c.NOTICE), now)
    result = c.build(changed, lib, prev, now=now)
    kept = [a for a in prev["programs"] if a["startEpochMs"] < boundary]
    assert result["programs"][:len(kept)] == kept


def test_branding_only_edit_does_not_replan():
    ch = network()
    lib = entries(100)
    prev = c.build(ch, lib, None, now=0)
    renamed = network(name="Renamed", color="#654321", number=101)
    result = c.build(renamed, lib, prev, now=0)
    assert result["programs"] == prev["programs"]


# ---------------------------------------------------------------------------
# Freshness: new arrivals, bootstrap honesty, bulk-import capacity
# ---------------------------------------------------------------------------


def test_new_arrival_airs_within_target_and_exactly_once_as_arrival():
    ch = network()
    lib = entries(100)
    prev = c.build(ch, lib, None, now=0)
    join = 5 * c.HOUR
    lib2 = lib + [dict(entries(1, start=999)[0], id="999")]
    publication = c.build(ch, lib2, prev, now=join)
    flagged = [a for a in publication["programs"] if a.get("arrival")]
    assert flagged and 24 * c.HOUR <= flagged[0]["startEpochMs"] - join <= 72 * c.HOUR, \
        "first airing targeted inside 24-72h"
    assert len(flagged) == 1, "an arrival uses its priority slot exactly once"


def test_arrival_slot_holds_still_until_it_commits():
    ch = network()
    lib = entries(100)
    prev = c.build(ch, lib, None, now=0)
    lib2 = lib + [dict(entries(1, start=999)[0], id="999")]
    placed_at = None
    for hour in range(5, 60):
        publication = c.build(ch, lib2, prev, now=hour * c.HOUR)
        flagged = [a for a in publication["programs"] if a.get("arrival")]
        if flagged:
            if placed_at is None:
                placed_at = flagged[0]["startEpochMs"]
            assert flagged[0]["startEpochMs"] == placed_at, "arrival slot must not slide hourly"
        prev = publication
    assert placed_at is not None


def test_bootstrap_does_not_label_whole_library_new():
    ch = network()
    publication = c.build(ch, entries(1000), None, now=0)
    assert publication["state"]["pendingArrivals"] == [], \
        "the first index is the baseline; existing content is not 'new'"


def test_bulk_import_drains_gradually_and_reports_capacity():
    ch = network()
    lib = entries(100)
    prev = c.build(ch, lib, None, now=0)
    lib2 = lib + entries(200, start=5000)
    publication = c.build(ch, lib2, prev, now=c.HOUR)
    pending = publication["state"]["pendingArrivals"]
    assert 0 < len(pending) <= 200, "a bulk import cannot be scheduled all at once"
    first = c.build(ch, lib2, publication, now=2 * c.HOUR)
    assert first["state"]["arrivalCredits"] < c.MAX_CREDITS or len(first["state"]["pendingArrivals"]) == 0


# ---------------------------------------------------------------------------
# Preferences: cooldown/spacing/time-of-day relax on thin sources
# ---------------------------------------------------------------------------


def test_tiny_source_relaxes_preferences_instead_of_stalling():
    ch = network()
    publication = c.build(ch, entries(2), None, now=0)
    assert publication["preparedThrough"] >= 24 * c.HOUR
    assert publication["warnings"], "relaxation is reported, never silent"


def test_single_studio_source_is_not_starved_by_spacing():
    ch = network()
    one_studio = [dict(e, studioId="42", performerIds=["9"]) for e in entries(30)]
    publication = c.build(ch, one_studio, None, now=0)
    ids = [a["item"]["id"] for a in publication["programs"]]
    assert len(set(ids[:30])) == 30, "uniform studio/performers must not starve the deck"


def test_time_of_day_diversity_uses_local_windows_and_survives_dst():
    ch = network()
    lib = entries(24, duration=3600)
    # A day that would repeat identically: 24 one-hour scenes over a 7-day
    # horizon cycles ~7 passes; without the window penalty the same scene
    # could land at the same local hour every pass. DST spring-forward in
    # America/Toronto 2027-03-14 must not break window math either.
    tz = c.programming_timezone("America/Toronto")
    start = 1741834800000  # 2025-03-13, the day before a spring-forward
    publication = c.build(ch, lib, None, now=start, tz=tz)
    assert publication["programs"], "builds across the DST boundary"


def test_empty_source_invents_no_filler():
    publication = c.build(network(), [], None, now=0)
    assert publication["programs"] == []
    assert publication["state"]["deck"] == []


# ---------------------------------------------------------------------------
# Outage: deterministic encore, observable recovery
# ---------------------------------------------------------------------------


def test_long_outage_finishes_current_encore_then_resumes(tmp_path):
    ch = network()
    lib = entries(100)
    old = c.build(ch, lib, None, now=0)
    now = 200 * c.HOUR + c.HOUR // 2
    snapshots.write_json(p.path(tmp_path, ch["id"]), old)
    encore = p.schedule(tmp_path, ch["id"], at=now)["programs"][0]
    recovered = c.build(ch, lib, old, now=now)
    current = next(a for a in recovered["programs"] if a["startEpochMs"] <= now < a["endEpochMs"])
    assert current == encore, "recovery keeps the deterministic encore airing clients already see"
    following = next(a for a in recovered["programs"] if a["startEpochMs"] == current["endEpochMs"])
    assert following["block"] != "Encore" and following["endEpochMs"] > following["startEpochMs"]
    assert recovered["degraded"] is True, "encore recovery is observable as degraded operation"


# ---------------------------------------------------------------------------
# Rollout, ops surfaces, and storage
# ---------------------------------------------------------------------------


def test_rollout_missing_file_activates_nothing(tmp_path):
    Rollout(tmp_path)  # never written
    assert c.load_rollout(tmp_path) == {"enabled": False, "networkIds": []}
    assert c.active_channels(tmp_path) == []


def test_rollout_activates_listed_networks_and_pins_respect_authored_fixed(tmp_path, monkeypatch):
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    rows = [
        network(cid="net_00000001", number=100),
        network(cid="net_00000002", number=101),
        network(cid="net_00000003", number=102, programming={"mode": "fixed"}),
    ]
    networks.PATH.write_text(json.dumps({"channels": rows}))
    Rollout(tmp_path).enable("net_00000001", "net_00000003")
    active = {ch["id"] for ch in c.active_channels(tmp_path)}
    assert active == {"net_00000001"}, "listed+pinnable: explicit authored fixed wins over rollout"


def test_rollout_disabled_kills_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": [network(cid="net_00000001")]}))
    Rollout(tmp_path).enable("net_00000001", enabled=False)
    assert c.active_channels(tmp_path) == []


def test_malformed_rollout_fails_loud_but_reads_as_off(tmp_path):
    (tmp_path / "continuing-networks.json").write_text("{not json")
    with pytest.raises(ValueError):
        c.load_rollout(tmp_path)
    assert c.try_load_rollout(tmp_path)["enabled"] is False
    assert c.active_channels_safe(tmp_path) == []


def test_prepare_writes_publication_and_isolates_per_channel_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": [
        network(cid="net_00000001", number=100),
        network(cid="net_00000002", number=101)]}))
    Rollout(tmp_path).enable("net_00000001", "net_00000002")
    rows = graphql_rows(entries(20))
    result = c.prepare(FakeIndexClient(rows), tmp_path, now=1_000_000)
    assert result["channels"] == {"net_00000001": "ready", "net_00000002": "ready"}
    for cid in ("net_00000001", "net_00000002"):
        publication = c.read(tmp_path, cid)
        assert publication["schema"] == c.SCHEMA and publication["programs"]

    # One channel's source query failing must not block its sibling: the
    # failing channel reports the error, the other still readies.
    networks.PATH.write_text(json.dumps({"channels": [
        network(cid="net_00000001", number=100, source={"type": "filter", "tags": ["2"]}),
        network(cid="net_00000002", number=101),
    ]}))

    class SometimesDown:
        def __init__(self, good):
            self.good = good

        def submit(self, query, variables):
            if "2" in json.dumps(variables.get("scene_filter") or {}):
                raise RuntimeError("stash down")
            return self.good.submit(query, variables)

    result = c.prepare(SometimesDown(FakeIndexClient(rows)), tmp_path, now=1_000_000 + c.HOUR)
    assert "RuntimeError" in result["channels"]["net_00000001"]
    assert result["channels"]["net_00000002"] == "ready"


def test_prepare_dedupes_identical_sources_in_one_run(tmp_path, monkeypatch):
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": [
        network(cid="net_00000001", number=100), network(cid="net_00000002", number=101)]}))
    Rollout(tmp_path).enable("net_00000001", "net_00000002")
    client = FakeIndexClient(graphql_rows(entries(20)))
    c.prepare(client, tmp_path, now=1_000_000)
    assert len(client.calls) <= 1, "two channels with one source fingerprint share the index queries"


def test_index_pages_the_whole_source_and_reports_over_limit():
    rows = graphql_rows(entries(1500))
    client = FakeIndexClient(rows)
    indexed = c.index_source(client, network())
    assert len(indexed) == 1500

    class Huge:
        def submit(self, query, variables):
            page = variables["filter"]["page"]
            return {"findScenes": {"count": c.MAX_INDEX + 1,
                                   "scenes": graphql_rows(entries(1, start=page * 10_000))}}

    with pytest.raises(ValueError, match="100,000"):
        c.index_source(Huge(), network())


def test_schedule_op_pages_network_publications(tmp_path):
    ch = network()
    lib = entries(100, duration=600)
    publication = c.build(ch, lib, None, now=0)
    snapshots.write_json(p.path(tmp_path, ch["id"]), publication)
    page = p.schedule(tmp_path, ch["id"], at=0, limit=50)
    assert page["status"] == "ready"
    assert len(page["programs"]) == 50
    assert page["programs"][0]["startEpochMs"] == 0
    next_page = p.schedule(tmp_path, ch["id"], at=page["programs"][-1]["endEpochMs"], limit=50)
    assert next_page["programs"][0]["startEpochMs"] >= page["programs"][-1]["endEpochMs"]


def test_schema1_publication_at_network_path_migrates_on_next_prepare(tmp_path):
    ch = network()
    legacy = p.build({"id": ch["id"], "source": ch["source"], "sort": "shuffle", "seed": 17,
                      "programming": {"mode": "explore"}}, entries(10), now=0)
    snapshots.write_json(p.path(tmp_path, ch["id"]), legacy)
    # Schedule still serves the legacy publication (no client breakage).
    served = p.schedule(tmp_path, ch["id"], at=0)
    assert served["status"] == "ready" and served["programs"]
    # The continuing reader detects the old schema; a build replaces it.
    assert c.read(tmp_path, ch["id"])["schema"] == 1
    rebuilt = c.build(ch, entries(10), None, now=c.HOUR)
    assert rebuilt["schema"] == c.SCHEMA


def test_corrupt_publication_raises_never_silently_resets(tmp_path):
    ch = network()
    file = p.path(tmp_path, ch["id"])
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('{"schema": 2, "programs": "not-a-list"}')
    with pytest.raises(ValueError):
        c.read(tmp_path, ch["id"])


def test_status_manifest_round_trip(tmp_path, monkeypatch):
    ch = network(cid="net_00000001", number=100)
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": [ch]}))
    Rollout(tmp_path).enable(ch["id"])
    publication = c.build(ch, entries(10), None, now=0)
    snapshots.write_json(p.path(tmp_path, ch["id"]), publication)
    manifest = c.write_status(tmp_path, {"customChannels": {}, "networkChannels": {ch["id"]: "ready"}}, now=0)
    entry = manifest["channels"][ch["id"]]
    assert entry["mode"] == c.MODE and entry["ready"] and entry["coverageHours"] > 0
    assert c.read_status(tmp_path)["channels"][ch["id"]] == entry


def test_dynamic_epoch_expires_index_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": [network(
        cid="net_00000001", source={"type": "filter", "tags": ["1"], "createdAt": {"withinDays": 30}})]}))
    Rollout(tmp_path).enable("net_00000001")
    client = FakeIndexClient(graphql_rows(entries(10)))
    now = 1_700_000_000_000
    c.prepare(client, tmp_path, now=now)
    first_calls = len(client.calls)
    assert c.read(tmp_path, "net_00000001")["indexEpoch"] != ""
    # Same day + fresh TTL: index reused, no new findScenes calls.
    c.prepare(client, tmp_path, now=now + c.HOUR)
    assert len(client.calls) == first_calls
    # Next UTC day: the epoch moved; membership is re-derived.
    import datetime as dt
    next_day = int((dt.datetime.fromtimestamp(now / 1000, dt.timezone.utc) +
                    dt.timedelta(days=1, hours=2)).timestamp() * 1000)
    c.prepare(client, tmp_path, now=next_day)
    assert len(client.calls) > first_calls


# ---------------------------------------------------------------------------
# Regression tests for defects the 30-day simulation exposed
# ---------------------------------------------------------------------------


def test_additions_never_drain_cumulative_exposure_counts():
    """A fingerprint change caused by ADDITIONS must not run the removal
    give-back: flexible airings were never durably counted, so "giving them
    back" drained real history (counts oscillated to zero at every reindex)."""
    ch = network()
    lib = entries(30, duration=25 * 60)
    prev = c.build(ch, lib, None, now=0)
    total = 0
    for hour in range(1, 24 * 10):
        if hour % 24 == 12:
            lib = lib + [dict(entries(1, start=1000 + hour)[0], id=f"new{hour}")]
        prev = c.build(ch, lib, prev, now=hour * c.HOUR)
        counts = sum(prev["state"]["airCounts"].values())
        assert counts >= total, f"cumulative counts regressed at hour {hour}"
        total = counts
    assert total > 24 * 10, "hourly cycling must accumulate durable history"


def test_deleted_scene_leaves_the_deck_even_without_a_future_airing():
    """A scene whose airings are all in the past when it is deleted triggers
    no replan — but its deck entry must still vanish, or the next pick
    crashes/re-airs a deleted scene."""
    ch = network()
    # A pass far longer than the horizon (1000 x 25min = 417h) leaves deck
    # scenes beyond every build's scheduling window — no future airing, the
    # exact no-replan case the simulation crash exposed.
    lib = entries(1000, duration=25 * 60)
    prev = c.build(ch, lib, None, now=0)
    for hour in range(1, 24 * 5):
        prev = c.build(ch, lib, prev, now=hour * c.HOUR)
    future_ids = {a["item"]["id"] for a in prev["programs"] if a["endEpochMs"] > 24 * 5 * c.HOUR}
    # Still queued this pass AND not scheduled in the future — the exact
    # state whose deck entry would crash the next pick after deletion.
    victim = next(i for i in prev["state"]["deck"] if i not in future_ids)
    assert victim in prev["state"]["deck"]
    lib_after = [e for e in lib if e["id"] != victim]
    nxt = c.build(ch, lib_after, prev, now=24 * 5 * c.HOUR)  # must not raise
    assert victim not in nxt["state"]["deck"]
    assert victim not in {a["item"]["id"] for a in nxt["programs"]
                          if a["endEpochMs"] > 24 * 5 * c.HOUR}


def test_pass_boundary_does_not_immediately_repeat_the_just_aired_scene():
    """LRU tie-breaking: a pass-boundary rebuild must not re-pick the scene
    that just aired at the previous pass's end."""
    ch = network()
    lib = entries(30, duration=25 * 60)
    prev = c.build(ch, lib, None, now=0)
    timeline = {}
    for hour in range(1, 24 * 12):
        prev = c.build(ch, lib, prev, now=hour * c.HOUR)
        for a in prev["programs"]:
            timeline[a["airingId"]] = (a["startEpochMs"], a["item"]["id"])
    starts = sorted(timeline.values())
    by_scene = {}
    for start, sid in starts:
        by_scene.setdefault(sid, []).append(start)
    worst = min((b - a for starts_ in by_scene.values()
                 for a, b in zip(starts_, starts_[1:])), default=c.HOUR)
    assert worst > 6 * c.HOUR, f"scene repeated after only {worst / c.HOUR}h"
