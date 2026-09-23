"""Regression tests for the 2026-09-22 independent review findings S1-S4.

These encode the FIXED behavior; the original observations live in
``analysis/continuing-review/recheck_v071.py`` (run against 5b33aa1).
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from justwatch import continuing as c, main as m, programming as p
from tests.test_continuing_remediation import entries, network

NOW = 1_800_000_000_000


def test_s1_replay_does_not_reconsume_completed_history():
    """S1: rebuilding selection state after completions must start from
    checkpoint + un-consumed reservations. The first new pick's consumption
    total equals the published program count — not the count plus the
    completed airings again."""
    ch = network()
    first = c.build(ch, entries(500), None, NOW)
    published = len(first["programs"])
    counts = []
    real_pick = c.pick_next

    def observe(working, *args, **kwargs):
        counts.append(sum(working["airCounts"].values()))
        return real_pick(working, *args, **kwargs)

    with patch.object(c, "pick_next", side_effect=observe):
        c.build(ch, entries(500), first, NOW + c.HOUR)
    assert counts, "extension must pick at least one new slot"
    assert counts[0] == published, (
        f"first extension pick consumed {counts[0]} exposures, expected "
        f"{published} (completed history was applied twice)")


def test_s1_arrival_rebuild_does_not_reconsume_either():
    """S1's second site: the arrival-driven rebuild replays the same
    reservations; it must not double-apply completed history either."""
    ch = network()
    first = c.build(ch, entries(500), None, NOW)
    counts = []
    real_pick = c.pick_next

    def observe(working, *args, **kwargs):
        counts.append(sum(working["airCounts"].values()))
        return real_pick(working, *args, **kwargs)

    with patch.object(c, "pick_next", side_effect=observe):
        pub = c.build(ch, entries(500), first, NOW + c.HOUR)
    baseline = counts[0]
    # Force an arrival rebuild: a genuinely new scene arrives.
    lib = entries(500) + [{"id": "brand-new", "title": "N", "duration": 1800.0,
                           "studio": "S", "studioId": "1", "performerIds": [],
                           "date": "2030-01-01", "createdAt": "2030-01-01T00:00:00+00:00"}]
    counts.clear()
    with patch.object(c, "pick_next", side_effect=observe):
        c.build(ch, lib, pub, NOW + 2 * c.HOUR)
    assert counts, "arrival rebuild must pick"
    assert counts[0] <= baseline + 1, (
        f"arrival rebuild started with {counts[0]} exposures "
        f"(baseline {baseline}): completed history was re-consumed")


@pytest.mark.parametrize("size", [2, 8])
def test_s2_thin_libraries_do_not_replay_the_just_ended_scene(size):
    """S2: within the repeat guard the MOST RECENTLY AIRED scene ranks last,
    so a thin library stops replaying the video that just ended."""
    ch = network()
    pub = c.build(ch, entries(size, plain=True), None, NOW)
    ids = [a["item"]["id"] for a in pub["programs"]]
    adjacent = [b for a, b in zip(ids, ids[1:]) if a == b]
    assert not adjacent, f"size {size}: immediate repeats in the 7-day publication: {adjacent}"


def test_s2_guard_order_varies_between_passes_without_strict_lru():
    """The rest of the guard tier follows the CURRENT pass's deck: consecutive
    passes neither repeat nor run one fixed permutation."""
    ch = network()
    pub = c.build(ch, entries(8, plain=True), None, NOW)
    ids = [a["item"]["id"] for a in pub["programs"]]
    p1, p2 = ids[:8], ids[8:16]
    assert p1 != p2, "pass order must vary"
    assert sorted(p1) == sorted(p2), "each pass covers the library once"


def test_s3_custom_channel_failure_fails_the_task():
    """S3: the old predicate compared a boolean to the string 'failure', so
    custom-channel errors never failed the task."""
    with patch.object(m.programming, "prepare",
                      return_value={"channels": {"ch_12345678": "index failed"}}), \
            patch.object(m.continuing, "prepare", return_value={"channels": {}}), \
            patch.object(m.continuing, "write_status"):
        with pytest.raises(Exception, match="could not be prepared"):
            m._op_prepare_programming(SimpleNamespace(
                client=None, data_dir=Path("/unused"), args={}))


def test_s3_rollout_error_fails_the_task():
    with patch.object(m.programming, "prepare", return_value={"channels": {}}), \
            patch.object(m.continuing, "prepare",
                         side_effect=ValueError("rollout file malformed")), \
            patch.object(m.continuing, "write_status"):
        with pytest.raises(Exception, match="could not be prepared"):
            m._op_prepare_programming(SimpleNamespace(
                client=None, data_dir=Path("/unused"), args={}))


def test_s3_legitimate_deferral_does_not_fail_the_task():
    class _Err(Exception):
        pass
    with patch.object(m.programming, "prepare", return_value={"channels": {}}), \
            patch.object(m.continuing, "prepare",
                         return_value={"channels": {"net_abcd1234": "deferred_index_budget"}}), \
            patch.object(m.continuing, "write_status"):
        result = m._op_prepare_programming(SimpleNamespace(
            client=None, data_dir=Path("/unused"), args={}))
    assert result["deferred"] == {"net_abcd1234": "deferred_index_budget"}


def test_s4_schema2_task_path_migration_preserves_the_current_airing():
    """S4: prepare() handed build() a None prior for schema-2 files, so the
    migration branch never saw the still-airing timeline. The task path now
    passes the real prior."""
    ch = network()
    lib = entries(100)
    legacy = {"schema": 2, "channelId": ch["id"], "mode": c.MODE,
              "configuration": "legacy",
              "programs": [{"airingId": "keep-current", "startEpochMs": NOW - c.HOUR,
                            "endEpochMs": NOW + c.HOUR, "item": lib[0], "block": ""}],
              "state": c.empty_state(NOW), "preparedThrough": NOW + c.HOUR,
              "index": lib, "indexIds": [e["id"] for e in lib],
              "warnings": [], "version": "old"}
    with patch.object(c, "active_channels", return_value=[ch]), \
            patch.object(c, "network_rows", return_value=[ch]), \
            patch.object(c, "index_source", return_value=lib), \
            patch.object(c, "try_load_rollout",
                         return_value={"enabled": True, "stage": "active",
                                       "networkIds": [ch["id"]]}):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            p.snapshots.write_json(p.path(tmp, ch["id"]), legacy)
            outcome = c.prepare(None, tmp, now=NOW)
            pub = c.read(tmp, ch["id"])
    assert outcome["channels"][ch["id"]] in ("ready", "recovered_from_outage")
    assert any(a["airingId"] == "keep-current" for a in pub["programs"]), (
        "the currently-airing program was dropped during schema-2 migration")
