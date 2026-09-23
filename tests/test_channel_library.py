"""Behavior tests for the authoritative channel library.

Integrity (strict load, no silent reset), transaction semantics (touched
records, receipts, idempotent retries, optimistic concurrency, identity
immutability), group constraints, and the absent-vs-empty distinction.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from justwatch import library

ROOT = Path(__file__).resolve().parents[1]


def base_library() -> dict:
    return {
        "schemaVersion": library.STORAGE_VERSION,
        "libraryId": "lib_test0000",
        "revision": 1,
        "groups": [
            {"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
            {"id": "grp_general", "name": "General", "position": 2, "legacySection": "general"},
        ],
        "channels": [
            {"id": "ch_11111111", "kind": "ch", "number": 1, "name": "One", "glyph": None,
             "color": "#112233", "groupId": "grp_my", "sort": "shuffle", "seed": 42,
             "enabled": True, "archived": False, "paused": False,
             "source": {"type": "tag", "id": "5", "ids": ["5"]}, "sourceLabel": "tag",
             "programming": {"mode": "fixed"}, "provenance": {"origin": "custom"}},
            {"id": "ch_22222222", "kind": "ch", "number": 2, "name": "Two", "glyph": None,
             "color": "#445566", "groupId": "grp_my", "sort": "shuffle", "seed": 7,
             "enabled": True, "archived": False, "paused": False,
             "source": {"type": "criteria", "tagsAny": ["1"], "excludeTags": ["9320"]},
             "sourceLabel": "", "programming": {"mode": "fixed"},
             "provenance": {"origin": "custom"}},
        ],
        "recentRequests": [],
    }


@pytest.fixture
def data_dir(tmp_path):
    library.save(tmp_path, base_library())
    return tmp_path


def test_missing_file_is_absent_not_empty(tmp_path):
    assert not library.exists(tmp_path)
    with pytest.raises(library.LibraryError):
        library.load(tmp_path)


def test_corrupt_library_raises_and_is_never_reset(data_dir):
    (data_dir / "channel-library.json").write_text("{not json")
    with pytest.raises(library.LibraryError):
        library.load(data_dir)
    raw = json.loads((data_dir / "channel-library.json").read_text().replace("{not json", "{}"))
    # a save of a VALID document still works after the operator fixes it
    library.save(data_dir, base_library())


def test_future_schema_version_fails_loudly(data_dir):
    doc = base_library()
    doc["schemaVersion"] = 99
    with pytest.raises(library.LibraryError, match="unsupported schemaVersion"):
        library.save(data_dir, doc)


def test_strict_integrity_unknown_fields_and_duplicates(data_dir):
    doc = base_library()
    doc["channels"][0]["mystery"] = 1
    with pytest.raises(library.LibraryError, match="unknown fields"):
        library.save(data_dir, doc)
    doc = base_library()
    doc["channels"][1]["number"] = 1
    with pytest.raises(library.LibraryError, match="held by both"):
        library.save(data_dir, doc)
    doc = base_library()
    doc["channels"][0]["enabled"] = "yes"
    with pytest.raises(library.LibraryError, match="real boolean"):
        library.save(data_dir, doc)
    doc = base_library()
    doc["channels"][0]["groupId"] = "grp_missing"
    with pytest.raises(library.LibraryError, match="unknown group"):
        library.save(data_dir, doc)


def test_transaction_touches_only_listed_records(data_dir):
    receipt = library.apply_transaction(
        data_dir, expected_revision=1, request_id="r1",
        ops=[{"op": "channel.put", "channel": {**json.loads(json.dumps(
            library.load(data_dir)["channels"][0])), "name": "Renamed"}}])
    assert receipt["status"] == "committed"
    doc = library.load(data_dir)
    names = {c["id"]: c["name"] for c in doc["channels"]}
    assert names["ch_11111111"] == "Renamed"
    assert names["ch_22222222"] == "Two", "an untouched channel must not change"


def test_receipt_replay_is_idempotent_even_with_stale_expected_revision(data_dir):
    ops = [{"op": "channels.patch", "channelIds": ["ch_11111111"], "patch": {"paused": True}}]
    first = library.apply_transaction(data_dir, expected_revision=1, request_id="same", ops=ops)
    assert first["status"] == "committed"
    # exact retry, even with a now-stale expectedRevision: same receipt
    replay = library.apply_transaction(data_dir, expected_revision=1, request_id="same", ops=ops)
    assert replay == first
    doc = library.load(data_dir)
    assert doc["revision"] == 2, "the replay must not bump the revision again"
    assert doc["channels"][0]["paused"] is True


def test_reused_request_id_with_different_payload_is_rejected(data_dir):
    a = [{"op": "channels.patch", "channelIds": ["ch_11111111"], "patch": {"paused": True}}]
    b = [{"op": "channels.patch", "channelIds": ["ch_11111111"], "patch": {"paused": False}}]
    assert library.apply_transaction(data_dir, expected_revision=1, request_id="dup", ops=a)["status"] == "committed"
    rejected = library.apply_transaction(data_dir, expected_revision=2, request_id="dup", ops=b)
    assert rejected["status"] == "rejected"
    assert rejected["error"] == "request_replayed_with_different_content"


def test_revision_conflict_returns_current_revision(data_dir):
    result = library.apply_transaction(
        data_dir, expected_revision=99, request_id="r",
        ops=[{"op": "channels.move", "channelIds": ["ch_11111111"], "groupId": "grp_general"}])
    assert result["status"] == "rejected"
    assert result["error"] == "revision_conflict"
    assert result["currentRevision"] == 1


def test_identity_is_immutable_through_edits(data_dir):
    doc = library.load(data_dir)
    draft = {**doc["channels"][0], "seed": 999999}
    result = library.apply_transaction(
        data_dir, expected_revision=1, request_id="r",
        ops=[{"op": "channel.put", "channel": draft}])
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "identity_change"
    stored = library.load(data_dir)["channels"][0]
    assert stored["seed"] == 42


def test_channel_create_assigns_identity_and_maps_temp_ids(data_dir):
    receipt = library.apply_transaction(
        data_dir, expected_revision=1, request_id="create",
        ops=[{"op": "channel.create", "tempId": "temp-abc", "channel": {
            "kind": "net", "number": 500, "name": "Fresh", "groupId": "grp_general",
            "source": {"type": "criteria", "tagsAny": ["3"]}}}])
    assert receipt["status"] == "committed"
    final_id = receipt["idMap"]["temp-abc"]
    assert final_id.startswith("net_")
    stored = library.get_receipt(data_dir, "create")
    assert stored["idMap"]["temp-abc"] == final_id


def test_group_constraints_last_group_and_destination(data_dir):
    library.apply_transaction(
        data_dir, expected_revision=1, request_id="seed",
        ops=[{"op": "channels.move", "channelIds": ["ch_11111111"], "groupId": "grp_general"}])
    result = library.apply_transaction(
        data_dir, expected_revision=2, request_id="r",
        ops=[{"op": "group.delete", "id": "grp_general"}])
    assert result["errors"][0]["code"] == "destination_required"
    result = library.apply_transaction(
        data_dir, expected_revision=2, request_id="r2",
        ops=[{"op": "group.delete", "id": "grp_general", "moveTo": "grp_my"},
             {"op": "group.delete", "id": "grp_my"}])
    assert result["errors"][0]["code"] == "destination_required", \
        "removing the second-to-last group still needs a destination"
    ok = library.apply_transaction(
        data_dir, expected_revision=2, request_id="r3",
        ops=[{"op": "group.delete", "id": "grp_general", "moveTo": "grp_my"}])
    assert ok["status"] == "committed"
    doc = library.load(data_dir)
    assert [c["groupId"] for c in doc["channels"]] == ["grp_my", "grp_my"]


def test_group_rename_is_rejected_when_duplicate_after_casefold(data_dir):
    result = library.apply_transaction(
        data_dir, expected_revision=1, request_id="r",
        ops=[{"op": "group.put", "group": {"id": "grp_general", "name": " my channels ", "position": 2}}])
    assert result["errors"][0]["code"] == "duplicate_group"


def test_swap_is_atomic_and_band_limited(data_dir):
    ok = library.apply_transaction(
        data_dir, expected_revision=1, request_id="swap",
        ops=[{"op": "channel.swap", "a": "ch_11111111", "b": "ch_22222222"}])
    assert ok["status"] == "committed"
    numbers = {c["id"]: c["number"] for c in library.load(data_dir)["channels"]}
    assert numbers == {"ch_11111111": 2, "ch_22222222": 1}


def test_history_archives_committed_revisions(data_dir):
    library.apply_transaction(
        data_dir, expected_revision=1, request_id="h1",
        ops=[{"op": "channels.patch", "channelIds": ["ch_11111111"], "patch": {"paused": True}}])
    entries = library.read_history(data_dir)["entries"]
    assert [e["revision"] for e in entries] == [2]
    snapshot_channels = {c["id"]: c for c in entries[0]["channels"]}
    assert snapshot_channels["ch_11111111"]["paused"] is True


def test_two_process_commit_race_is_serialized(data_dir):
    """Two processes race the same expectedRevision with DIFFERENT request
    ids: exactly one commits, the other gets revision_conflict. (A shared
    request id would legitimately replay the same receipt instead.)"""
    def worker(request_id: str) -> str:
        script = (
            "import sys, json;"
            "sys.path.insert(0, %r);"
            "from justwatch import library;"
            "r = library.apply_transaction(%r, expected_revision=1, request_id=%r,"
            " ops=[{'op':'channels.patch','channelIds':['ch_11111111'],'patch':{'paused':True}}]);"
            "print(json.dumps({'status': r['status'], 'revision': r.get('revision')}))" % (
                str(ROOT), str(data_dir), request_id)
        )
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, check=True)
        return json.loads(out.stdout)["status"]

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(worker, ["race-a", "race-b"]))
    assert sorted(statuses) == ["committed", "rejected"]
    assert library.load(data_dir)["revision"] == 2


def test_intentionally_empty_library_is_distinct_from_absent(tmp_path):
    doc = base_library()
    doc["channels"] = []
    library.save(tmp_path, doc)
    assert library.exists(tmp_path), "present-but-empty is authoritative"
    view_channels = library.load(tmp_path)["channels"]
    assert view_channels == []


def test_put_onto_taken_number_is_a_typed_error_not_a_crash(data_dir):
    """Regression: a put targeting another channel's number used to crash the
    validator (NameError on the swap-pair lookup) instead of reporting."""
    result = library.apply_transaction(
        data_dir, expected_revision=1, request_id="taken",
        ops=[{"op": "channel.put", "channel": {**json.loads(json.dumps(
            library.load(data_dir)["channels"][0])), "number": 2}}])
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "duplicate_number"
