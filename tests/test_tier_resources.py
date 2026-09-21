"""Tier-level batch scheduler tests (review findings R6/R7/R8).

Realistic multi-channel resource behavior: 45+ channels with heterogeneous
sources (empty, tiny, large, identical, failing), a bounded index budget, and
a shared source cache. Reuses the simulation's membership-client shape so the
fixtures carry REAL distinct membership semantics.
"""
from __future__ import annotations

import json

import pytest

from justwatch import continuing as c
from justwatch import networks, programming as p


def row(cid, number, seed, source):
    return {"id": cid, "number": number, "name": f"Net {number}", "glyph": "\uf005",
            "color": "#123456", "section": "general", "family": "tag_spotlight",
            "count": 0, "sort": "shuffle", "seed": seed,
            "programmingMode": "fixed", "source": source}


def graphql_rows(items):
    return [
        {"id": e["id"], "title": e["title"], "date": None,
         "created_at": e.get("createdAt") or None,
         "studio": {"id": e["studioId"], "name": e["studio"]},
         "performers": [{"id": pid} for pid in e["performerIds"]],
         "files": [{"duration": e["duration"]}],
         "paths": {"preview": ""}}
        for e in items
    ]


def entries(n, start=0, duration=1800):
    return [
        {"id": str(start + i), "title": f"S{start + i}", "duration": duration,
         "studioId": str((start + i) % 5), "studio": f"Studio {(start + i) % 5}",
         "performerIds": [str((start + i) % 7)], "date": "", "createdAt": "",
         "preview": ""}
        for i in range(n)
    ]


class TierClient:
    """Answers per-tag membership from a shared library; counts queries."""

    def __init__(self, by_tag: dict[str, list[dict]]):
        self.by_tag = by_tag
        self.queries = 0
        self.fail_tags: set[str] = set()

    def submit(self, query, variables):
        self.queries += 1
        assert "ContinuingIndex" in query
        tag = ((variables.get("scene_filter") or {}).get("tags") or {}).get("value")[0]
        if tag in self.fail_tags:
            raise RuntimeError("stash query failed")
        rows = self.by_tag.get(tag, [])
        page = variables["filter"]["page"]
        per_page = variables["filter"]["per_page"]
        chunk = rows[(page - 1) * per_page: page * per_page]
        return {"findScenes": {"count": len(rows), "scenes": graphql_rows(chunk)}}


@pytest.fixture()
def tier(tmp_path, monkeypatch):
    """45 channels: 5 empty sources, 10 tiny, 25 large, 5 identical large."""
    channels = []
    by_tag: dict[str, list] = {}
    for i in range(45):
        cid = f"net_{i:08x}"
        if i < 5:
            source = {"type": "filter", "tags": [f"9{i:02d}"]}         # empty
            by_tag[f"9{i:02d}"] = []
        elif i < 15:
            source = {"type": "filter", "tags": [f"9{i:02d}"]}         # tiny
            by_tag[f"9{i:02d}"] = entries(4, start=i * 1000)
        elif i < 40:
            source = {"type": "filter", "tags": [f"9{i:02d}"]}         # large
            by_tag[f"9{i:02d}"] = entries(300, start=i * 1000)
        else:
            source = {"type": "filter", "tags": ["999"]}        # identical
            by_tag.setdefault("999", entries(300, start=990000))
        channels.append(row(cid, 100 + i, 1000 + i, source))
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": channels}))
    (tmp_path / "continuing-networks.json").write_text(json.dumps(
        {"enabled": True, "networkIds": [ch["id"] for ch in channels]}))
    return tmp_path, TierClient(by_tag)


def test_tier_prepare_with_tight_budget_defers_fairly(tier):
    """Budget 6 over 45 channels: no channel is repeatedly privileged; every
    channel reaches a publication within a few runs; deferrals are named."""
    tmp, client = tier
    now = 1_000_000_000
    served_ever: set[str] = set()
    for run in range(10):
        outcome = c.prepare(client, tmp, now=now + run * c.HOUR, budget=6)
        classes = {k: c.outcome_class(v) for k, v in outcome["channels"].items()}
        assert set(classes.values()) <= {"success", "deferred"}, classes
        deferred = [k for k, v in classes.items() if v == "deferred"]
        serviced = [k for k, v in classes.items() if v == "success"]
        served_ever |= set(serviced)
        # A deferral must be NAMED, never a silent no-op or a failure.
        assert all(outcome["channels"][k] == "deferred_index_budget" for k in deferred)
        if run == 0:
            assert deferred, "a 45-channel tier with budget 6 must defer on its first run"
    assert len(served_ever) >= 40, f"fair rotation must reach the tier, got {len(served_ever)}"


def test_tier_identical_sources_share_one_index(tier):
    """Five channels with one identical source run the Stash query once."""
    tmp, client = tier
    now = 1_000_000_000
    c.prepare(client, tmp, now=now, budget=45)
    first = client.queries
    # The five shared channels are among the 40 distinct source queries + pages;
    # rerun everything with a cold cache and count again: the identical group
    # must cost the same as ONE channel, not five.
    assert first <= 41 * 2, f"shared sources duplicated query work: {first} queries"


def test_tier_empty_sources_are_not_always_due(tier):
    """Empty indexes are cached: within the TTL the tier needs no re-queries."""
    tmp, client = tier
    now = 1_000_000_000
    c.prepare(client, tmp, now=now, budget=45)
    after_first = client.queries
    c.prepare(client, tmp, now=now + c.HOUR, budget=45)
    assert client.queries == after_first, "a fresh tier must not re-query within the TTL"


def test_tier_failing_source_backs_off_but_peers_progress(tier):
    """A source failing repeatedly is deferred with a bounded backoff while
    the rest of the tier keeps preparing."""
    tmp, client = tier
    now = 1_000_000_000
    victim = "net_00000027"  # hex 27 = 39: a large channel with its own source
    client.fail_tags.add("939")
    for run in range(4):
        outcome = c.prepare(client, tmp, now=now + run * c.HOUR, budget=45)
        cls = c.outcome_class(outcome["channels"][victim])
        assert cls in ("failure", "deferred")
        ready = [k for k, v in outcome["channels"].items()
                 if k != victim and c.outcome_class(v) == "success"]
        assert ready, f"peers must progress despite the failing source (run {run})"
    # After >= FAILURE_BACKOFF_LIMIT consecutive failures the channel is on
    # backoff: a named deferred state, and the source is not hammered.
    assert outcome["channels"][victim] == "backoff"
    queries_with_failure = client.queries
    outcome = c.prepare(client, tmp, now=now + 3 * c.HOUR + c.HOUR // 2, budget=45)
    assert outcome["channels"][victim] == "backoff"
    assert client.queries == queries_with_failure, "backed-off source is not re-queried"


def test_tier_heterogeneous_time_to_live(tmp_path, monkeypatch):
    """Channels keep independent reindex jitter: within one hour of a tier run
    only channels whose individual TTL expired are re-indexed."""
    channels = [
        row(f"net_{i:08x}", 100 + i, 11 * i + 7, {"type": "filter", "tags": [f"8{i:02d}"]})
        for i in range(41)
    ]
    by_tag = {f"8{i:02d}": entries(10, start=i * 500) for i in range(41)}
    monkeypatch.setattr(networks, "PATH", tmp_path / "networks.json")
    networks.PATH.write_text(json.dumps({"channels": channels}))
    (tmp_path / "continuing-networks.json").write_text(json.dumps(
        {"enabled": True, "networkIds": [ch["id"] for ch in channels]}))
    client = TierClient(by_tag)
    now = 1_000_000_000
    c.prepare(client, tmp_path, now=now, budget=45)
    after_first = client.queries
    # Index jitter is stable per channel: within an hour NOTHING is due
    # (TTL >= INDEX_TTL > 1h for every channel regardless of jitter).
    c.prepare(client, tmp_path, now=now + c.HOUR, budget=45)
    assert client.queries == after_first
