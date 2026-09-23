#!/usr/bin/env python3
"""Offline observations for the second independent review (plugin 5b33aa1).

No network calls, product changes, or real publication writes. Reports current
behavior; this is not an acceptance test asserting defects as desired behavior.
"""
import json
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from justwatch import continuing as c, main as m, programming as p  # noqa: E402


def main():
    fixtures = runpy.run_path(str(ROOT / "tests/test_continuing_remediation.py"))
    channel, entries = fixtures["network"], fixtures["entries"]
    ch, now = channel(), 1_800_000_000_000
    results = {}

    first = c.build(ch, entries(500), None, now)
    counts = []
    real_pick = c.pick_next

    def observe(working, *args, **kwargs):
        counts.append(sum(working["airCounts"].values()))
        return real_pick(working, *args, **kwargs)

    with patch.object(c, "pick_next", side_effect=observe):
        c.build(ch, entries(500), first, now+c.HOUR)
    results["replay_exposure_count"] = {
        "expected_before_extension": len(first["programs"]), "actual": counts[0]}

    repeats = {}
    for n in (2, 8, 30, 100):
        pub = c.build(ch, entries(n, plain=True), None, now)
        ids = [a["item"]["id"] for a in pub["programs"]]
        repeats[str(n)] = sum(a == b for a, b in zip(ids, ids[1:]))
    results["immediate_repeats_in_seven_days"] = repeats

    with patch.object(m.programming, "prepare", return_value={"channels": {"ch_12345678": "index failed"}}), \
            patch.object(m.continuing, "prepare", return_value={"channels": {}}), \
            patch.object(m.continuing, "write_status"):
        try:
            result = m._op_prepare_programming(SimpleNamespace(client=None, data_dir=Path("/unused"), args={}))
            results["custom_failure"] = {"task_raised": False, "returned_channels": result["channels"]}
        except Exception as exc:
            results["custom_failure"] = {"task_raised": True, "error": str(exc)}

    lib = entries(100)
    legacy = {"schema": 2, "channelId": ch["id"], "mode": c.MODE,
              "configuration": "legacy", "programs": [{"airingId": "keep-current",
                  "startEpochMs": now-c.HOUR, "endEpochMs": now+c.HOUR, "item": lib[0], "block": ""}],
              "state": c.empty_state(now), "preparedThrough": now+c.HOUR, "index": lib,
              "indexIds": [e["id"] for e in lib], "warnings": [], "version": "old"}
    with tempfile.TemporaryDirectory() as tmp, patch.object(c, "active_channels", return_value=[ch]), \
            patch.object(c.networks, "get", return_value=ch), \
            patch.object(c, "index_source", return_value=lib), \
            patch.object(c, "try_load_rollout", return_value={"enabled": True, "stage": "active", "networkIds": [ch["id"]]}):
        p.snapshots.write_json(p.path(tmp, ch["id"]), legacy)
        outcome = c.prepare(None, tmp, now=now)
        pub = c.read(tmp, ch["id"])
        results["schema2_migration"] = {"outcome": outcome["channels"][ch["id"]],
            "preserves_current_airing": any(a["airingId"] == "keep-current" for a in pub["programs"])}

    sim = runpy.run_path(str(ROOT / "tools/simulate_continuing.py"))
    ids = list(map(str, range(100)))
    ids[50] = "0"
    airings = [(now+i*c.HOUR, now+(i+1)*c.HOUR, sid) for i, sid in enumerate(ids)]
    results["duplicate_oracle"] = {"first_pass_size": len(ids), "unique": len(set(ids)),
        "detected": sim["duplicate_airings"](airings, lambda _: 100)}

    pub = c.build(ch, lib, None, now)
    manifest = {"channels": {ch["id"]: c.channel_status(pub, c.MODE)}}
    live = c.status_view(manifest, now+200*c.HOUR)["channels"][ch["id"]]
    results["expired_status"] = {k: live[k] for k in ("coverageHours", "ready", "expiring", "encore")}
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
