"""End-to-end tests for tools/migrate_channel_library.py.

Run on disposable synthetic deployments: seed fidelity (513 networks, exact
292/221/282 reconciliation, the proposal's five JAV exceptions), custom
preservation, no-op rerun after owner edits, drift detection, and restore.
"""
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/migrate_channel_library.py"
EXPECTED_EXCEPTIONS = {225, 252, 407, 461, 480}


def run_tool(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--data-dir", str(data_dir), *args],
        capture_output=True, text=True)


@pytest.fixture(scope="module")
def deployment(tmp_path_factory):
    """A synthetic legacy deployment: the real 795 compiled tier + customs."""
    tmp = tmp_path_factory.mktemp("migrate")
    data = tmp / "data"
    data.mkdir()
    shutil.copy2(ROOT / "justwatch/networks.json", data / "networks.json")
    catalog = {
        "schemaVersion": 1, "revision": 21,
        "settings": {"soloThreshold": 10, "groupThreshold": 5, "launchMode": "last",
                     "includeTags": {"general": [], "studios": [], "performers": []},
                     "excludeTags": {"general": [], "studios": [], "performers": []}},
        "channels": [
            {"id": "ch_aaa11111", "number": 1, "name": "Custom One", "glyph": "\uf111",
             "color": "#112233", "source": {"type": "tag", "id": "5", "ids": ["5", "7"]},
             "sourceLabel": "tags", "sort": "shuffle", "seed": 424242, "enabled": True,
             "programming": {"mode": "explore", "spacing": 2}},
            {"id": "ch_bbb22222", "number": 2, "name": "Custom Two", "glyph": "\uf005",
             "color": "#445566", "source": {"type": "savedFilter", "id": "7"},
             "sourceLabel": "saved", "sort": "newest", "seed": 99, "enabled": False,
             "programming": {"mode": "fixed"}},
        ],
    }
    (data / "catalog.json").write_text(json.dumps(catalog))
    return tmp, data


@pytest.fixture(scope="module")
def migrated(deployment):
    tmp, data = deployment
    result = run_tool(data, "--staging-dir", str(tmp / "staging"), "--apply")
    assert result.returncode == 0, result.stderr
    return tmp, data


def test_seed_fidelity_all_513_identities_and_groups(migrated):
    tmp, data = migrated
    doc = json.loads((data / "channel-library.json").read_text())
    nets = [c for c in doc["channels"] if c["kind"] == "net"]
    assert len(nets) == 513
    preview = json.loads((ROOT / "analysis/just-watch-final/networks.preview.json").read_text())
    by_id = {c["id"]: c for c in nets}
    for row in preview["channels"]:
        mine = by_id[row["id"]]
        assert mine["number"] == row["number"]
        assert mine["seed"] == row["seed"]
        assert mine["source"] == row["source"]
        assert mine["name"] == row["name"]
    sections = {}
    for c in nets:
        sections[c["provenance"]["legacySection"]] = sections.get(c["provenance"]["legacySection"], 0) + 1
    assert sections == {"general": 209, "studios": 115, "performers": 189}


def test_seed_fidelity_survivor_identities_are_exact(migrated):
    tmp, data = migrated
    doc = json.loads((data / "channel-library.json").read_text())
    by_number = {c["number"]: c for c in doc["channels"]}
    with (ROOT / "analysis/just-watch-final/migration_map.csv").open(newline="") as fh:
        keeps = [r for r in csv.DictReader(fh) if r["disposition"] == "keep"]
    assert len(keeps) == 292
    for row in keeps:
        channel = by_number[int(row["current_number"])]
        assert channel["provenance"]["stableKey"] == row["final_channel_key"]


def test_seed_fidelity_five_sanctioned_exceptions(migrated):
    tmp, data = migrated
    doc = json.loads((data / "channel-library.json").read_text())
    nets = [c for c in doc["channels"] if c["kind"] == "net"]
    excluding_jav = {c["number"] for c in nets
                     if "9320" in (c["source"].get("excludeTags") or [])}
    assert len(nets) - len(excluding_jav) == 5
    assert set(range(100, 900)) - excluding_jav and True
    no_exclusion = {c["number"] for c in nets} - excluding_jav
    assert no_exclusion == EXPECTED_EXCEPTIONS


def test_custom_preservation_ids_seeds_sources_policies(migrated):
    tmp, data = migrated
    doc = json.loads((data / "channel-library.json").read_text())
    customs = {c["id"]: c for c in doc["channels"] if c["kind"] == "ch"}
    assert set(customs) == {"ch_aaa11111", "ch_bbb22222"}
    one = customs["ch_aaa11111"]
    assert one["seed"] == 424242
    assert one["source"] == {"type": "tag", "id": "5", "ids": ["5", "7"]}
    assert one["programming"]["mode"] == "explore"
    assert one["groupId"] == "grp_my"
    assert customs["ch_bbb22222"]["enabled"] is False


def test_no_op_rerun_after_owner_edit(deployment, migrated):
    tmp, data = deployment
    from justwatch import library as libmod
    libmod.apply_transaction(data, expected_revision=1, request_id="owner-edit", ops=[
        {"op": "channels.move", "channelIds": ["ch_aaa11111"], "groupId": "grp_general"}])
    doc_before = json.loads((data / "channel-library.json").read_text())
    result = run_tool(data, "--staging-dir", str(tmp / "staging"), "--apply")
    assert result.returncode == 0
    assert "NO-OP" in result.stderr
    doc_after = json.loads((data / "channel-library.json").read_text())
    assert doc_after == doc_before, "a rerun must never reset owner edits"


def test_drift_fails_closed_for_dry_run_and_apply(tmp_path):
    """Audit C10: drift means nonzero exit and NO authoritative write — for
    dry-run and apply alike — with the drift named precisely."""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy2(ROOT / "justwatch/networks.json", data / "networks.json")
    (data / "catalog.json").write_text(json.dumps(
        {"schemaVersion": 1, "revision": 3, "settings": {}, "channels": []}))
    # identity drift on a KEEP slot + an unexplained ghost on a vacant number
    import csv as _csv
    with (ROOT / "analysis/just-watch-final/migration_map.csv").open(newline="") as fh:
        keep_row = next(r for r in _csv.DictReader(fh) if r["disposition"] == "keep")
    live = json.loads((data / "networks.json").read_text())
    taken = {row["number"] for row in live["channels"]}
    vacant = next(n for n in range(100, 900) if n not in taken)
    for row in live["channels"]:
        if row["number"] == int(keep_row["current_number"]):
            row["name"] = "Renamed Beyond Recognition"
            break
    live["channels"].append({
        "id": "net_deadbeef", "number": vacant, "name": "Ghost Network",
        "glyph": "\uf111", "color": "#000000", "section": "general",
        "family": "test", "count": 1, "sort": "shuffle", "seed": 1,
        "programmingMode": "fixed", "sourceLabel": "",
        "source": {"type": "filter", "tags": ["5"]}})
    (data / "networks.json").write_text(json.dumps(live))
    dry = run_tool(data, "--staging-dir", str(tmp_path / "staging"), "--dry-run")
    assert dry.returncode == 2, dry.stderr
    # identity drift is reported precisely...
    assert "Renamed Beyond Recognition" in dry.stderr
    assert keep_row["current_name"] in dry.stderr
    # ...and the ghost is drift (no map entry), while nothing was written
    assert "Ghost Network" in dry.stderr
    assert not (data / "channel-library.json").exists()
    apply_result = run_tool(data, "--staging-dir", str(tmp_path / "staging"), "--apply")
    assert apply_result.returncode == 2
    assert not (data / "channel-library.json").exists()
    assert not list(tmp_path.glob("migration-backup-*")), \
        "drift must abort before any backup or commit"


def test_restore_brings_back_the_legacy_deployment_exactly(deployment, migrated):
    tmp, data = deployment
    backups = sorted(tmp.glob("migration-backup-*"))
    assert backups, "the apply run must leave a backup"
    # post-backup artifacts within the managed scope must NOT survive restore
    (data / "programming").mkdir(exist_ok=True)
    (data / "programming" / "newer.json").write_text('{"new": true}')
    (data / "continuing-networks.json").write_text(
        json.dumps({"enabled": True, "networkIds": ["net_00000001"]}))
    result = run_tool(data, "--restore", str(backups[-1]))
    assert result.returncode == 0, result.stderr
    assert (data / "catalog.json").exists()
    catalog = json.loads((data / "catalog.json").read_text())
    assert catalog["revision"] == 21
    # the library (absent at backup time) is removed, and the post-backup
    # rollout/publication do not overlay the restored state
    assert not (data / "channel-library.json").exists()
    assert not (data / "continuing-networks.json").exists()
    assert not (data / "programming" / "newer.json").exists()

def test_rollout_preserved_for_survivors_and_recorded_for_retired(tmp_path):
    """The rollout file's networkIds (NOT 'channels') drive activation
    preservation; retired ids are recorded explicitly, never re-numbered."""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy2(ROOT / "justwatch/networks.json", data / "networks.json")
    (data / "catalog.json").write_text(json.dumps(
        {"schemaVersion": 1, "revision": 21, "settings": {}, "channels": []}))
    preview = json.loads(
        (ROOT / "analysis/just-watch-final/networks.preview.json").read_text())
    survivor = preview["channels"][0]["id"]
    (data / "continuing-networks.json").write_text(json.dumps({
        "enabled": True, "stage": "active",
        "networkIds": [survivor, "net_8091d3ea"]}))
    result = run_tool(data, "--staging-dir", str(tmp_path / "staging"), "--apply")
    assert result.returncode == 0, result.stderr
    doc = json.loads((data / "channel-library.json").read_text())
    assert doc["migration"]["retiredRolloutIds"] == ["net_8091d3ea"]
    # the rollout file itself is preserved verbatim in the backup
    backups = sorted(tmp_path.glob("migration-backup-*"))
    assert json.loads((backups[-1] / "continuing-networks.json").read_text())[
        "networkIds"] == [survivor, "net_8091d3ea"]
