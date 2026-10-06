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


# ---------------------------------------------------------------------------
# channels.renumber (the arrangement opcode) + final-occupancy validation
# ---------------------------------------------------------------------------


def net_doc() -> dict:
    """Three consecutive networks at 300-302 plus one custom at 12."""
    doc = base_library()
    for i, number in enumerate((300, 301, 302)):
        doc["channels"].append({
            "id": f"net_aaaa000{i + 1}", "kind": "net", "number": number,
            "name": f"Net {number}", "glyph": None, "color": "#112233",
            "groupId": "grp_general", "sort": "shuffle", "seed": 100 + i,
            "enabled": True, "archived": False, "paused": False,
            "source": {"type": "tag", "id": "5", "ids": ["5"]},
            "sourceLabel": "", "programming": {"mode": "fixed"},
            "provenance": {"origin": "custom"}})
    return doc


@pytest.fixture
def net_dir(tmp_path):
    library.save(tmp_path, net_doc())
    return tmp_path


NET1, NET2, NET3 = "net_aaaa0001", "net_aaaa0002", "net_aaaa0003"


def numbers_of(data_dir):
    return {c["id"]: c["number"] for c in library.load(data_dir)["channels"]}


def test_renumber_cycles_and_swaps_are_legal(net_dir):
    """A simultaneous final-number map: rotations and 2-cycles pass without
    any clean-draft prerequisite."""
    receipt = library.apply_transaction(
        net_dir, expected_revision=1, request_id="cycle",
        ops=[{"op": "channels.renumber", "assignments": [
            {"channelId": NET1, "number": 302},
            {"channelId": NET2, "number": 300},
            {"channelId": NET3, "number": 301}]}])
    assert receipt["status"] == "committed"
    numbers = numbers_of(net_dir)
    assert [numbers[NET1], numbers[NET2], numbers[NET3]] == [302, 300, 301]
    # a renumber 2-cycle is a swap without the swap opcode
    receipt = library.apply_transaction(
        net_dir, expected_revision=2, request_id="pair",
        ops=[{"op": "channels.renumber", "assignments": [
            {"channelId": NET2, "number": 301},
            {"channelId": NET3, "number": 300}]}])
    assert receipt["status"] == "committed"
    numbers = numbers_of(net_dir)
    assert numbers[NET2] == 301 and numbers[NET3] == 300


def test_renumber_standalone_swap_shape_alone_still_works(data_dir):
    receipt = library.apply_transaction(
        data_dir, expected_revision=1, request_id="swap",
        ops=[{"op": "channel.swap", "a": "ch_11111111", "b": "ch_22222222"}])
    assert receipt["status"] == "committed", "the opcode stays supported"


def test_final_occupancy_rejects_untouched_holders_and_duplicates(net_dir):
    occupied = library.apply_transaction(
        net_dir, expected_revision=1, request_id="held",
        ops=[{"op": "channels.renumber", "assignments": [
            {"channelId": NET1, "number": 302}]}])
    assert occupied["status"] == "rejected"
    assert occupied["errors"][0]["code"] == "destination_occupied"
    assert "Net 302" in occupied["errors"][0]["message"]
    # the packet CAN resolve it: move the holder away in the same transaction
    resolved = library.apply_transaction(
        net_dir, expected_revision=1, request_id="held-ok",
        ops=[{"op": "channels.renumber", "assignments": [
            {"channelId": NET1, "number": 302},
            {"channelId": NET3, "number": 303}]}])
    assert resolved["status"] == "committed"
    dup = library.apply_transaction(
        net_dir, expected_revision=2, request_id="dup",
        ops=[{"op": "channels.renumber", "assignments": [
            {"channelId": NET2, "number": 303},
            {"channelId": NET3, "number": 303}]}])
    assert dup["status"] == "rejected"
    codes = [e["code"] for e in dup["errors"]]
    assert "duplicate_destination" in codes
    # a rejected packet leaves definitions and revision byte-identical
    assert library.load(net_dir)["revision"] == 2


def test_renumber_bad_payloads_are_typed(net_dir):
    def attempt(rid, ops):
        return library.apply_transaction(
            net_dir, expected_revision=1, request_id=rid, ops=ops)

    r = attempt("e1", [{"op": "channels.renumber", "assignments": []}])
    assert r["errors"][0]["code"] == "empty_assignment"
    r = attempt("e2", [{"op": "channels.renumber"}])
    assert r["errors"][0]["code"] == "empty_assignment"
    r = attempt("e3", [{"op": "channels.renumber",
                        "assignments": [{"channelId": NET1, "number": 300,
                                         "why": 1}]}])
    assert r["errors"][0]["code"] == "bad_assignment"
    r = attempt("e4", [{"op": "channels.renumber", "assignments": ["nope"]}])
    assert r["errors"][0]["code"] == "bad_assignment"
    r = attempt("e5", [{"op": "channels.renumber",
                        "assignments": [{"channelId": "net_00000000",
                                         "number": 300}]}])
    assert r["errors"][0]["code"] == "unknown_channel"
    r = attempt("e6", [{"op": "channels.renumber",
                        "assignments": [{"channelId": NET1, "number": True}]}])
    assert r["errors"][0]["code"] == "bad_number"
    r = attempt("e7", [{"op": "channels.renumber",
                        "assignments": [{"channelId": NET1, "number": 12}]}])
    assert r["errors"][0]["code"] == "cross_band"
    r = attempt("e8", [{"op": "channels.renumber", "assignments": [
        {"channelId": NET1, "number": 303},
        {"channelId": NET1, "number": 304}]}])
    assert r["errors"][0]["code"] == "ambiguous_assignment"
    r = attempt("e9", [{"op": "channel.swap", "a": NET1, "b": NET2},
                       {"op": "channels.renumber", "assignments": [
                           {"channelId": NET1, "number": 305}]}])
    assert r["errors"][0]["code"] == "ambiguous_assignment"
    stored = next(c for c in library.load(net_dir)["channels"]
                  if c["id"] == NET1)
    r = attempt("e10", [
        {"op": "channel.put", "channel": {**json.loads(json.dumps(stored)),
                                          "number": 306}},
        {"op": "channels.renumber", "assignments": [
            {"channelId": NET1, "number": 305}]}])
    assert r["errors"][0]["code"] == "ambiguous_assignment"
    # agreement is redundant-but-consistent: accepted
    r = attempt("e11", [
        {"op": "channel.put", "channel": {**json.loads(json.dumps(stored)),
                                          "number": 305}},
        {"op": "channels.renumber", "assignments": [
            {"channelId": NET1, "number": 305}]}])
    assert r["status"] == "committed"


def test_create_into_number_vacated_by_same_packet_renumber(net_dir):
    """Create at occupied 300 while the run shifts up: ONE Apply, ONE
    revision — the arrangement row of the acceptance matrix."""
    ops = [
        {"op": "channel.create", "tempId": "temp-1", "channel": {
            "kind": "net", "number": 300, "name": "Fresh",
            "groupId": "grp_general",
            "source": {"type": "criteria", "tagsAny": ["3"]}}},
        {"op": "channels.renumber", "assignments": [
            {"channelId": NET1, "number": 301},
            {"channelId": NET2, "number": 302},
            {"channelId": NET3, "number": 303}]},
    ]
    receipt = library.apply_transaction(
        net_dir, expected_revision=1, request_id="insert-300", ops=ops)
    assert receipt["status"] == "committed"
    assert receipt["revision"] == 2, "exactly one revision increment"
    new_id = receipt["idMap"]["temp-1"]
    numbers = numbers_of(net_dir)
    assert numbers[new_id] == 300
    assert numbers[NET1] == 301 and numbers[NET2] == 302 and numbers[NET3] == 303
    # ...and the mirror: validate agrees with apply on the same shape
    check = library._check_ops(library.load(net_dir), [
        {"op": "channels.renumber", "assignments": [
            {"channelId": new_id, "number": 299},
            {"channelId": NET1, "number": 300}]}])
    assert check == [], "validate and apply see the same final plan"


def test_put_name_plus_renumber_swap_in_one_packet(net_dir):
    """The dirty-name channel trades numbers with a neighbour through ONE
    packet: the put carries the edit, renumber carries the map — no
    clean-draft prerequisite, no swap opcode."""
    stored = {c["id"]: c for c in library.load(net_dir)["channels"]}
    draft = {**json.loads(json.dumps(stored[NET1])),
             "name": "Dirty Name", "number": 302}
    ops = [
        {"op": "channel.put", "channel": draft},
        {"op": "channels.renumber", "assignments": [
            {"channelId": NET3, "number": 300}]},
    ]
    # final map: NET1=302, NET2=301, NET3=300 — each destination's original
    # holder moves away in the same packet, so nothing is "taken".
    receipt = library.apply_transaction(
        net_dir, expected_revision=1, request_id="dirty-swap", ops=ops)
    assert receipt["status"] == "committed"
    numbers = numbers_of(net_dir)
    assert numbers[NET1] == 302 and numbers[NET3] == 300
    names = {c["id"]: c["name"] for c in library.load(net_dir)["channels"]}
    assert names[NET1] == "Dirty Name"


def test_group_create_move_renumber_atomic_and_rejection_clean(net_dir):
    ops = [
        {"op": "group.put", "group": {"id": "grp_performers",
                                      "name": "Performers", "position": 3}},
        {"op": "channels.move", "channelIds": [NET1, NET2],
         "groupId": "grp_performers"},
        {"op": "channels.renumber", "assignments": [
            {"channelId": NET1, "number": 310},
            {"channelId": NET2, "number": 311}]},
    ]
    receipt = library.apply_transaction(
        net_dir, expected_revision=1, request_id="combo", ops=ops)
    assert receipt["status"] == "committed"
    doc = library.load(net_dir)
    groups = {g["name"]: g["id"] for g in doc["groups"]}
    assert "Performers" in groups
    moved = {c["id"]: (c["groupId"], c["number"]) for c in doc["channels"]}
    assert moved[NET1] == (groups["Performers"], 310)
    assert moved[NET2] == (groups["Performers"], 311)
    assert moved[NET3] == ("grp_general", 302)
    # rejection mid-combination: nothing moves, revision unchanged
    bad = [
        {"op": "group.put", "group": {"id": "grp_later", "name": "Later",
                                      "position": 4}},
        {"op": "channels.move", "channelIds": [NET3],
         "groupId": "grp_later"},
        {"op": "channels.renumber", "assignments": [
            {"channelId": NET3, "number": 1}]},
    ]
    rejected = library.apply_transaction(
        net_dir, expected_revision=2, request_id="combo-bad", ops=bad)
    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["code"] == "cross_band"
    doc = library.load(net_dir)
    assert doc["revision"] == 2
    assert all(g["name"] != "Later" for g in doc["groups"]), "no partial commit"


def test_renumber_replay_and_conflict(net_dir):
    ops = [{"op": "channels.renumber", "assignments": [
        {"channelId": NET1, "number": 305}]}]
    first = library.apply_transaction(
        net_dir, expected_revision=1, request_id="same", ops=ops)
    assert first["status"] == "committed"
    replay = library.apply_transaction(
        net_dir, expected_revision=1, request_id="same", ops=ops)
    assert replay == first, "exact retry replays the stored receipt"
    assert library.load(net_dir)["revision"] == 2
    conflict = library.apply_transaction(
        net_dir, expected_revision=1, request_id="other", ops=ops)
    assert conflict["status"] == "rejected"
    assert conflict["error"] == "revision_conflict"
    assert conflict["currentRevision"] == 2
    changed = [{"op": "channels.renumber", "assignments": [
        {"channelId": NET1, "number": 306}]}]
    diverged = library.apply_transaction(
        net_dir, expected_revision=2, request_id="same", ops=changed)
    assert diverged["error"] == "request_replayed_with_different_content"


def test_renumber_multi_band_single_op(net_dir):
    """Frozen decision: ONE renumber op carries per-band assignments; each
    channel is band-checked against its OWN kind."""
    ops = [{"op": "channels.renumber", "assignments": [
        {"channelId": "ch_11111111", "number": 12},
        {"channelId": NET1, "number": 299}]}]
    receipt = library.apply_transaction(
        net_dir, expected_revision=1, request_id="bands", ops=ops)
    assert receipt["status"] == "committed"
    numbers = numbers_of(net_dir)
    assert numbers["ch_11111111"] == 12 and numbers[NET1] == 299


def test_put_with_unknown_id_is_a_typed_error(net_dir):
    """Regression guard: a put naming a nonexistent id at a FREE number used
    to pass validation and crash staging with a KeyError."""
    ops = [{"op": "channel.put", "channel": {
        "id": "net_00000000", "kind": "net", "number": 305, "name": "Ghost",
        "groupId": "grp_general", "sort": "shuffle",
        "source": {"type": "tag", "id": "5", "ids": ["5"]}}}]

    errors = library._check_ops(library.load(net_dir), ops)
    assert [e["code"] for e in errors] == ["unknown_channel"]
    result = library.apply_transaction(
        net_dir, expected_revision=1, request_id="ghost", ops=ops)
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "unknown_channel"
