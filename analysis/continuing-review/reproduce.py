#!/usr/bin/env python3
"""Read-only review probes. Writes only a JSON report when stdout is redirected.

Run from any directory: python3 analysis/continuing-review/reproduce.py
These report observed behavior, not passing acceptance assertions. All publication
files and mocks are local temporary fixtures; no Stash or TV connection is made.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from justwatch import continuing as c, programming as p  # noqa: E402

NOW = 1_800_000_000_000


def channel(cid="net_aabbccdd"):
    return {"id": cid, "number": 100, "seed": 17, "sort": "shuffle",
            "source": {"type": "filter", "tags": ["1"]}}


def entries(n=100, duration=1800):
    return [{"id": str(i), "title": "Synthetic", "duration": duration,
             "studioId": str(i % 5), "studio": "Synthetic",
             "performerIds": [str(i % 7)], "createdAt": "", "date": "",
             "preview": ""} for i in range(n)]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    out = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(out)
    return out


def main():
    results = {}
    ch, lib = channel(), entries()
    first = c.build(ch, lib, None, NOW)
    pub = first
    for hour in range(1, 49):
        pub = c.build(ch, lib, pub, NOW + hour * c.HOUR)
    results["aired_history_48h"] = {
        "retained_completed_airings": sum(a["endEpochMs"] <= NOW + 48*c.HOUR for a in pub["programs"]),
        "recorded_intervals": sum(map(len, pub["aired"].values()))}

    changed = dict(ch, programming={"spacing": 3})
    pub = c.build(changed, lib, first, NOW + c.HOUR)
    protected = [a for a in first["programs"] if a["endEpochMs"] > NOW+c.HOUR
                 and a["startEpochMs"] < NOW+24*c.HOUR]
    results["policy_edit"] = {"protected": len(protected),
                              "changed": sum(a not in pub["programs"] for a in protected)}

    prev = c.build(ch, lib, first, NOW+c.HOUR)
    victim = next(a["item"]["id"] for a in prev["programs"] if a["startEpochMs"] == NOW+12*c.HOUR)
    pub = c.build(ch, [e for e in lib if e["id"] != victim], prev, NOW+2*c.HOUR)
    cut = [a for a in prev["programs"] if NOW+12*c.HOUR <= a["startEpochMs"] < NOW+25*c.HOUR
           and a["item"]["id"] != victim]
    results["deletion_rollback"] = {
        "canceled_eligible_airings": len(cut),
        "canceled_ids_still_missing_from_deck": sum(a["item"]["id"] not in pub["state"]["deck"] for a in cut),
        "committedThrough_hour": (pub["committedThrough"]-NOW)/c.HOUR,
        "actual_last_committed_hour": (pub["programs"][pub["committedCount"]-1]["startEpochMs"]-NOW)/c.HOUR}

    late = NOW + 240*c.HOUR + 123456
    pub = c.build(ch, lib, first, late)
    with tempfile.TemporaryDirectory() as tmp:
        p.snapshots.write_json(p.path(tmp, ch["id"]), first)
        encore = p.schedule(tmp, ch["id"], at=late)["programs"][0]
    current = next(a for a in pub["programs"] if a["startEpochMs"] <= late < a["endEpochMs"])
    results["long_outage"] = {"degraded": pub["degraded"], "preserves_current_encore": current == encore}

    try:
        c.build(ch, entries(2, 10), None, NOW)
        results["short_clips"] = "no exception"
    except Exception as exc:
        results["short_clips"] = f"{type(exc).__name__}: {exc}"

    lib_plain = [dict(e, studioId="", performerIds=[]) for e in entries(100)]
    pub = c.build(ch, lib_plain, None, NOW)
    ids = [a["item"]["id"] for a in pub["programs"]]
    results["order_100_scenes"] = {"identical_first_two_passes": ids[:100] == ids[100:200]}

    idx = {sid: dict(entries(1)[0], id=sid) for sid in ("a", "b")}
    state = c.empty_state(NOW)
    state.update(deck=["a", "b"], arrivalCredits=2., pendingArrivals=[{"id": "a", "firstSeenAt": NOW}])
    sid, arrival, _ = c._pick_next(state, idx, {}, NOW, c.programming_timezone(), c.network_policy(ch))
    c.fold_airing(state, {"item": idx[sid], "startEpochMs": NOW, "arrival": arrival}, ch, idx)
    results["arrival_credit"] = {"expected_after_one_slot": 1.15, "actual": state["arrivalCredits"]}
    state = c.empty_state(NOW)
    state.update(deck=["b"], arrivalCredits=1., pendingArrivals=[{"id": "a", "firstSeenAt": NOW}])
    results["arrival_outside_deck"] = c._pick_next(state, idx, {}, NOW, c.programming_timezone(), c.network_policy(ch))[0]

    chs = [channel(f"net_{i:08x}") for i in range(3)]
    with tempfile.TemporaryDirectory() as tmp, patch.object(c, "active_channels", return_value=chs), \
            patch.object(c.networks, "get", side_effect=lambda cid: next(x for x in chs if x["id"] == cid)), \
            patch.object(c, "index_source", return_value=[]):
        results["budget_runs"] = [c.prepare(None, tmp, now=NOW+h*c.HOUR, budget=2)["channels"] for h in range(3)]
        results["budget_cursor"] = c._load_scheduler(tmp)
    with tempfile.TemporaryDirectory() as tmp, patch.object(c, "active_channels", return_value=chs):
        bad = p.path(tmp, chs[0]["id"])
        bad.parent.mkdir(parents=True)
        bad.write_text("{broken")
        try:
            c.prepare(None, tmp, now=NOW)
            results["corrupt_channel"] = "isolated"
        except Exception as exc:
            results["corrupt_channel"] = f"whole prepare raised {type(exc).__name__}"

    sim = module("review_sim", "tools/simulate_continuing.py")
    results["daily_repeat_gap_expected_24h"] = sim.repeat_gaps([
        (NOW+i*24*c.HOUR, NOW+(i*24+1)*c.HOUR, "one") for i in range(4)])

    cli = module("review_cli", "tools/prepare_programming.py")
    with tempfile.TemporaryDirectory() as tmp:
        keyfile = Path(tmp)/"dummy-key"
        keyfile.write_text("fixture-not-a-real-key")
        buf = io.StringIO()
        with patch.object(sys, "argv", ["prepare_programming.py", "--url", "http://fixture.invalid",
                                      "--api-key-file", str(keyfile), "--verify"]), \
                patch.object(cli, "query", side_effect=[{"runPluginTask": "new-job"}, {"jobQueue": []},
                    {"runPluginOperation": {"generatedAt": 1, "lastRun": {"networkChannels": {"old": "ready"}}}}]), \
                contextlib.redirect_stdout(buf):
            cli.main()
        results["verify_accepts_stale_unrelated_run"] = "verified: preparation task completed" in buf.getvalue()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
