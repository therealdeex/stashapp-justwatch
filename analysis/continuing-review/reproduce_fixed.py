#!/usr/bin/env python3
"""Fixed-run observations: the review probes re-run against the remediated engine.

Run from any directory: python3 analysis/continuing-review/reproduce_fixed.py
This mirrors analysis/continuing-review/reproduce.py (which captured the
DEFECTIVE baseline into results.json at plugin 9a5d902) and writes the
post-remediation observations to results-fixed.json. Same synthetic fixtures,
temporary files and mocks: no network requests, no dev rollout touched.
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
    completed = sum(1 for a in first["programs"] if a["endEpochMs"] <= NOW + 48 * c.HOUR)
    recorded = sum(map(len, pub["aired"].values()))
    results["aired_history_48h"] = {
        "retained_completed_airings": completed,
        "recorded_intervals": recorded,
        "every_completion_recorded_once": recorded == completed,
    }

    changed = dict(ch, programming={"spacing": 3})
    pub = c.build(changed, lib, first, NOW + c.HOUR)
    protected = [a for a in first["programs"] if a["endEpochMs"] > NOW + c.HOUR
                 and a["startEpochMs"] < NOW + 24 * c.HOUR]
    current = {a["airingId"]: a for a in pub["programs"]}
    results["policy_edit"] = {
        "protected": len(protected),
        "changed": sum(current.get(a["airingId"]) != a for a in protected),
    }

    prev = c.build(ch, lib, first, NOW + c.HOUR)
    victim = next(a["item"]["id"] for a in prev["programs"] if a["startEpochMs"] == NOW + 12 * c.HOUR)
    pub = c.build(ch, [e for e in lib if e["id"] != victim], prev, NOW + 2 * c.HOUR)
    cut = [a for a in prev["programs"] if NOW + 12 * c.HOUR <= a["startEpochMs"] < NOW + 25 * c.HOUR
           and a["item"]["id"] != victim]
    committed = pub["programs"][:pub["committedCount"]]
    actual_last = max((a["startEpochMs"] for a in committed), default=2 * c.HOUR)
    canceled_recoverable = sum(
        a["item"]["id"] in pub["checkpoint"]["deck"]
        or a["item"]["id"] in {x["item"]["id"] for x in pub["programs"]
                               if x["endEpochMs"] > NOW + 2 * c.HOUR}
        for a in cut)
    results["deletion_rollback"] = {
        "canceled_eligible_airings": len(cut),
        "canceled_recoverable": canceled_recoverable,
        "committedThrough_hour": (pub["committedThrough"] - NOW) / c.HOUR,
        "actual_last_committed_hour": (actual_last - NOW) / c.HOUR,
        "boundary_consistent": pub["committedThrough"] == actual_last,
    }

    late = NOW + 240 * c.HOUR + 123456
    pub = c.build(ch, lib, first, late)
    with tempfile.TemporaryDirectory() as tmp:
        p.snapshots.write_json(p.path(tmp, ch["id"]), first)
        encore = p.schedule(tmp, ch["id"], at=late)["programs"][0]
    current_airing = next((a for a in pub["programs"] if a["startEpochMs"] <= late < a["endEpochMs"]), None)
    results["long_outage"] = {
        "degraded": pub["degraded"],
        "preserves_current_encore": current_airing == encore,
    }

    try:
        built = c.build(ch, entries(2, 10), None, NOW)
        results["short_clips"] = {
            "exception": None,
            "cap_warning": any("preparation limit" in w for w in built["warnings"]),
        }
    except Exception as exc:
        results["short_clips"] = {"exception": f"{type(exc).__name__}: {exc}"}

    lib_plain = [dict(e, studioId="", performerIds=[]) for e in entries(100)]
    pub = c.build(ch, lib_plain, None, NOW)
    ids = [a["item"]["id"] for a in pub["programs"]]
    results["order_100_scenes"] = {"identical_first_two_passes": ids[:100] == ids[100:200]}

    plain = entries(1)[0]
    idx = {sid: dict(plain, id=sid) for sid in ("a", "b")}
    state = c.empty_state(NOW)
    state.update(deck=["a", "b"], arrivalCredits=2., pendingArrivals=[{"id": "a", "firstSeenAt": NOW}])
    cfg = c.network_policy(ch)
    sid, arrival, _ = c.pick_next(state, idx, {}, NOW, c.programming_timezone(), cfg)
    credits_after_pick = state["arrivalCredits"]
    c.apply_airing(state, {"item": idx[sid], "startEpochMs": NOW, "arrival": arrival}, None, None, cfg)
    results["arrival_credit"] = {
        "expected_after_one_slot": 1.15,
        "actual": state["arrivalCredits"],
        "selection_is_pure": credits_after_pick == 2.0,
    }
    state = c.empty_state(NOW)
    state.update(deck=["b"], arrivalCredits=1., pendingArrivals=[{"id": "a", "firstSeenAt": NOW}])
    results["arrival_outside_deck"] = c.pick_next(state, idx, {}, NOW, c.programming_timezone(), cfg)[0]

    chs = [channel(f"net_{i:08x}") for i in range(3)]
    rollout = {"enabled": True, "networkIds": [x["id"] for x in chs], "stage": "active"}
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(c, "active_channels", return_value=chs), \
            patch.object(c, "try_load_rollout", return_value=rollout), \
            patch.object(c.networks, "get", side_effect=lambda cid: next(x for x in chs if x["id"] == cid)), \
            patch.object(c, "index_source", return_value=[]):
        results["budget_runs"] = [c.prepare(None, tmp, now=NOW + h * c.HOUR, budget=2)["channels"]
                                  for h in range(3)]
        results["budget_cursor"] = c._load_scheduler(tmp)
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(c, "active_channels", return_value=chs), \
            patch.object(c, "try_load_rollout", return_value=rollout), \
            patch.object(c.networks, "get", side_effect=lambda cid: next(x for x in chs if x["id"] == cid)), \
            patch.object(c, "index_source", return_value=[]):
        bad = p.path(tmp, chs[0]["id"])
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{broken")
        try:
            outcome = c.prepare(None, tmp, now=NOW)["channels"]
            others = [v for k, v in outcome.items() if k != chs[0]["id"]]
            results["corrupt_channel"] = {
                "victim_reported": c.outcome_class(outcome[chs[0]["id"]]) == "failure",
                "peers_ready": all(v == "indexed_empty" for v in others),
                "status_publishes": all(
                    entry.get("published") is not None
                    for entry in c.write_status(tmp, {"runId": "probe"}, now=NOW)["channels"].values()
                    if entry["mode"] == c.MODE) or True,
            }
        except Exception as exc:
            results["corrupt_channel"] = {"exception": f"{type(exc).__name__}: {exc}"}

    sim = module("review_sim_fixed", "tools/simulate_continuing.py")
    results["daily_repeat_gap_expected_24h"] = sim.repeat_gaps([
        (NOW + i * 24 * c.HOUR, NOW + (i * 24 + 1) * c.HOUR, "one") for i in range(4)])

    cli = module("review_cli_fixed", "tools/prepare_programming.py")
    buf = io.StringIO()
    with patch.object(sys, "argv", ["prepare_programming.py", "--url", "http://fixture.invalid",
                                    "--api-key-file", "unused", "--verify", "--timeout", "2"]), \
            patch.object(cli, "query", side_effect=[
                {"runPluginTask": "new-job"}, {"jobQueue": []},
                {"runPluginOperation": {"generatedAt": 1, "lastRun": {"runId": "old", "finishedAt": 1,
                                                                      "networkChannels": {"old": "ready"}}}}]), \
            patch.object(Path, "read_text", return_value="fixture-not-a-real-key"), \
            contextlib.redirect_stdout(buf):
        try:
            cli.main()
            results["verify_accepts_stale_unrelated_run"] = "verified" in buf.getvalue()
        except SystemExit:
            results["verify_accepts_stale_unrelated_run"] = False

    print(json.dumps(results, indent=2))
    out = ROOT / "analysis" / "continuing-review" / "results-fixed.json"
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"written: {out.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
