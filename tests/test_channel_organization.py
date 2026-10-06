"""The pure arrangement planner: deterministic number/group planning.

Every case feeds a committed document + ONE intent into
``organization.plan`` (no I/O, no fixtures on disk) and asserts the exact
plan: number changes with reasons, displaced bystanders, capacity math,
choices for the shared number-resolution sheet, typed errors, and the
submit-ready packet. Byte-identical output for identical input is itself a
test (determinism property below).
"""
import copy
import json

from justwatch import organization


def net_row(number, name=None, *, cid=None, group="grp_net", **over):
    row = {
        "id": cid or f"net_{number:08x}",
        "kind": "net", "number": number, "name": name or f"Net {number}",
        "glyph": None, "color": "#112233", "groupId": group,
        "sort": "shuffle", "seed": number, "enabled": True,
        "archived": False, "paused": False,
        "source": {"type": "tag", "id": "5", "ids": ["5"]},
        "sourceLabel": "", "programming": {"mode": "fixed"},
        "provenance": {"origin": "custom"},
    }
    row.update(over)
    return row


def ch_row(number, name=None, **over):
    over.setdefault("cid", f"ch_{number:08d}")
    over.setdefault("kind", "ch")
    return net_row(number, name, group="grp_my", **over)


def doc_of(*channels, revision=12, library_id="lib_org0001"):
    return {
        "schemaVersion": 1, "libraryId": library_id, "revision": revision,
        "groups": [
            {"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
            {"id": "grp_net", "name": "Networks", "position": 2, "legacySection": None},
        ],
        "channels": sorted(channels, key=lambda c: c["number"]),
        "recentRequests": [],
    }


def ids_by_number(doc):
    return {c["number"]: c["id"] for c in doc["channels"]}


def moves_by_ref(body):
    return {(c["channelId"] or c["tempRef"]): (c["from"], c["to"], c["reason"],
                                               c["selected"])
            for c in body["numberChanges"]}


def ids300():  # three consecutive networks at 300-302
    return doc_of(net_row(300, "Alpha"), net_row(301, "Beta"), net_row(302, "Gamma"))


# ---------------------------------------------------------------------------
# insert + the explain/choices block (shared number-resolution sheet)
# ---------------------------------------------------------------------------


def test_insert_up_shifts_run_and_reports_choices():
    doc = ids300()
    body = organization.plan(
        doc, overlays=[{"tempRef": "temp-1", "kind": "net", "name": "New"}],
        intent={"type": "insert", "tempRef": "temp-1", "number": 300},
        explain=True)
    assert body["valid"] and not body["noop"]
    got = moves_by_ref(body)
    assert got["temp-1"] == (None, 300, "insert-shift-up", True)
    assert got["net_0000012c"][0:2] == (300, 301)  # the run shifts, in review
    assert got["net_0000012d"][0:2] == (301, 302)
    assert got["net_0000012e"][0:2] == (302, 303)
    assert [c["channelId"] for c in body["displaced"]] == [
        "net_0000012c", "net_0000012d", "net_0000012e"]
    assert body["choices"]["swap"]["available"] is False
    assert body["choices"]["shiftUp"] == {"available": True, "firstFree": 303,
                                          "movedCount": 3}
    assert body["choices"]["shiftDown"]["available"] is True
    assert body["choices"]["free"]["nextHigher"] == 303
    # packet: create skeleton FIRST, then the renumber; skeleton carries no source
    assert body["packet"]["expectedRevision"] == 12
    ops = body["packet"]["ops"]
    assert [op["op"] for op in ops] == ["channel.create", "channels.renumber"]
    assert ops[0]["channel"] == {"kind": "net", "number": 300, "name": "New"}
    assert ops[1]["assignments"] == [
        {"channelId": "net_0000012c", "number": 301},
        {"channelId": "net_0000012d", "number": 302},
        {"channelId": "net_0000012e", "number": 303}]
    assert body["created"] == [{"tempRef": "temp-1", "number": 300, "groupId": None}]


def test_insert_down_mirror():
    doc = ids300()
    body = organization.plan(
        doc, overlays=[{"tempRef": "temp-1", "kind": "net"}],
        intent={"type": "insert", "tempRef": "temp-1", "number": 300,
                "direction": "down"})
    got = moves_by_ref(body)
    assert got["net_0000012c"] == (300, 299, "insert-shift-down", False)
    assert got["temp-1"] == (None, 300, "insert-shift-down", True)
    assert set(got) == {"temp-1", "net_0000012c"}


def test_insert_mover_removed_from_occupancy_first():
    # 305 moves into occupied 300: its own old slot is NOT an obstacle and the
    # cascade stops at the first genuinely free number (302).
    doc = doc_of(net_row(300, "Alpha"), net_row(301, "Beta"), net_row(305, "Mover"))
    body = organization.plan(
        doc, intent={"type": "insert", "channelId": "net_00000131",
                     "number": 300})
    got = moves_by_ref(body)
    assert got["net_00000131"] == (305, 300, "insert-shift-up", True)
    assert got["net_0000012c"] == (300, 301, "insert-shift-up", False)
    assert got["net_0000012d"] == (301, 302, "insert-shift-up", False)
    assert set(got) == {"net_00000131", "net_0000012c", "net_0000012d"}


def test_insert_temp_no_swap_but_relocate_and_free_number():
    # acceptance: new-channel sheet — swap absent, relocation/free available
    doc = ids300()
    body = organization.plan(
        doc, overlays=[{"tempRef": "temp-1", "kind": "net"}],
        intent={"type": "insert", "tempRef": "temp-1", "number": 300},
        explain=True)
    choices = body["choices"]
    assert choices["swap"]["available"] is False
    assert "no original slot" in choices["swap"]["reason"]
    assert choices["relocate"]["available"] is True
    assert choices["free"]["list"]


def test_insert_existing_swap_choice_names_holder():
    doc = ids300()
    body = organization.plan(
        doc, intent={"type": "insert", "channelId": "net_0000012c",
                     "number": 302},
        explain=True)  # the 300 row inserts at 302 (held by Gamma)
    assert body["choices"]["swap"] == {
        "available": True, "with": "net_0000012e", "withName": "Gamma",
        "withNumber": 302}


def test_insert_free_target_is_direct_and_choices_decline():
    doc = doc_of(net_row(300, "Alpha"))
    body = organization.plan(
        doc, intent={"type": "insert", "channelId": "net_0000012c",
                     "number": 305},
        explain=True)
    assert body["valid"] and not body["noop"]
    assert moves_by_ref(body)["net_0000012c"] == (300, 305, "direct", True)
    assert body["choices"]["swap"]["available"] is False
    assert body["choices"]["shiftUp"]["available"] is False
    assert body["choices"]["relocate"]["available"] is False


def test_insert_full_band_is_honest():
    rows = [ch_row(n) for n in range(1, 100)]  # the whole ch band occupied
    doc = doc_of(*rows)
    body = organization.plan(
        doc, overlays=[{"tempRef": "temp-1", "kind": "ch"}],
        intent={"type": "insert", "tempRef": "temp-1", "number": 50},
        explain=True)
    assert body["valid"] is False
    assert body["errors"][0]["code"] == "no_capacity"
    assert "No free number above 99." in body["errors"][0]["message"]
    assert body["choices"]["shiftUp"] == {"available": False, "firstFree": None,
                                          "movedCount": None,
                                          "reason": "No free number above 99."}
    assert body["capacity"]["shortfall"] == 1
    assert body["packet"] is None and body["numberChanges"] == []


# ---------------------------------------------------------------------------
# move_block
# ---------------------------------------------------------------------------


def test_move_block_preserves_order_and_reviews_displaced():
    doc = doc_of(net_row(171, "A"), net_row(173, "B"), net_row(180, "C"),
                 net_row(300, "X"), net_row(301, "Y"), net_row(302, "Z"))
    body = organization.plan(
        doc, intent={"type": "move_block", "start": 300,
                     "channelIds": ["net_000000ab", "net_000000ad",
                                    "net_000000b4"]})
    assert body["valid"] and not body["noop"]
    got = moves_by_ref(body)
    # the block lands consecutively in the GIVEN order
    assert [got["net_000000ab"], got["net_000000ad"], got["net_000000b4"]] == [
        (171, 300, "direct", True), (173, 301, "direct", True),
        (180, 302, "direct", True)]
    # bystanders keep their relative order and land just above the block
    assert got["net_0000012c"] == (300, 303, "block-displaced", False)
    assert got["net_0000012d"] == (301, 304, "block-displaced", False)
    assert got["net_0000012e"] == (302, 305, "block-displaced", False)
    assert body["packet"]["expectedRevision"] == 12
    assert body["packet"]["ops"] == [{"op": "channels.renumber", "assignments": [
        {"channelId": "net_000000ab", "number": 300},
        {"channelId": "net_000000ad", "number": 301},
        {"channelId": "net_000000b4", "number": 302},
        {"channelId": "net_0000012c", "number": 303},
        {"channelId": "net_0000012d", "number": 304},
        {"channelId": "net_0000012e", "number": 305}]}]


def test_move_block_cascade_flows_only_through_free_positions():
    # the block of one (the 299 row) displaces X@300; Y@301 is OUTSIDE the
    # block span, so X pushes past it to the first genuinely free slot (302).
    doc = doc_of(net_row(299, "A"), net_row(300, "X"), net_row(301, "Y"))
    body = organization.plan(
        doc, intent={"type": "move_block", "start": 300,
                     "channelIds": ["net_0000012b"]})
    got = moves_by_ref(body)
    assert got["net_0000012b"] == (299, 300, "direct", True)
    assert got["net_0000012c"] == (300, 302, "block-displaced", False)
    assert "net_0000012d" not in got


def test_move_block_rejects_mixed_band_and_nonfit():
    doc = doc_of(net_row(300), net_row(301), ch_row(5, cid="ch_00000005"))
    body = organization.plan(
        doc, intent={"type": "move_block", "start": 300,
                     "channelIds": ["net_0000012c", "ch_00000005"]})
    assert body["errors"][0]["code"] == "band_mixed"
    assert "ch_00000005" in body["errors"][0]["message"]
    body = organization.plan(
        doc, intent={"type": "move_block", "start": 899,
                     "channelIds": ["net_0000012c", "net_0000012d"]})
    assert body["errors"][0]["code"] == "cross_band"


def test_move_block_mixed_existing_and_temp_rows_places_all():
    # a draft row in the block takes its consecutive slot like an existing
    # row: planned at 302, reported in `created`, emitted as a create skeleton
    # that carries the planned number — never silently dropped.
    doc = doc_of(net_row(171, "A"), net_row(173, "B"), net_row(400, "Z"))
    body = organization.plan(
        doc, overlays=[{"tempRef": "temp-1", "kind": "net", "name": "New One",
                        "groupId": "grp_net"}],
        intent={"type": "move_block", "start": 300,
                "channelIds": ["net_000000ab", "net_000000ad"],
                "tempRefs": ["temp-1"]})
    assert body["valid"] and not body["noop"]
    got = moves_by_ref(body)
    assert got["net_000000ab"] == (171, 300, "direct", True)
    assert got["net_000000ad"] == (173, 301, "direct", True)
    assert got["temp-1"] == (None, 302, "direct", True)
    assert body["created"] == [{"tempRef": "temp-1", "number": 302,
                                "groupId": "grp_net"}]
    assert body["packet"]["expectedRevision"] == 12
    ops = body["packet"]["ops"]
    assert [op["op"] for op in ops] == ["channel.create", "channels.renumber"]
    assert ops[0] == {"op": "channel.create", "tempId": "temp-1",
                      "channel": {"kind": "net", "number": 302,
                                  "name": "New One", "groupId": "grp_net"}}
    assert ops[1]["assignments"] == [
        {"channelId": "net_000000ab", "number": 300},
        {"channelId": "net_000000ad", "number": 301}]


def test_move_block_temp_kind_comes_from_overlay():
    doc = doc_of(net_row(300), net_row(301))
    body = organization.plan(
        doc, overlays=[{"tempRef": "temp-1", "kind": "ch"}],
        intent={"type": "move_block", "start": 300,
                "channelIds": ["net_0000012c"], "tempRefs": ["temp-1"]})
    assert body["valid"] is False
    assert body["errors"][0]["code"] == "band_mixed"
    assert "temp-1" in body["errors"][0]["message"]
    assert body["packet"] is None
    body = organization.plan(
        doc, intent={"type": "move_block", "start": 300,
                     "channelIds": ["net_0000012c"],
                     "tempRefs": ["no-such-overlay"]})
    assert body["valid"] is False
    assert body["errors"][0]["code"] == "unknown_temp_ref"


# ---------------------------------------------------------------------------
# shift_interval
# ---------------------------------------------------------------------------


def test_shift_interval_preserves_gaps():
    doc = doc_of(net_row(200), net_row(202), net_row(205))
    body = organization.plan(
        doc, intent={"type": "shift_interval",
                     "range": {"start": 200, "end": 202}, "offset": 10})
    got = moves_by_ref(body)
    assert got["net_000000c8"] == (200, 210, "shift-interval", True)
    assert got["net_000000ca"] == (202, 212, "shift-interval", True)
    assert set(got) == {"net_000000c8", "net_000000ca"}, "205 is outside"


def test_shift_interval_blockers_are_typed_errors():
    doc = doc_of(net_row(200), net_row(202), net_row(210, "Blocker"))
    body = organization.plan(
        doc, intent={"type": "shift_interval",
                     "range": {"start": 200, "end": 202}, "offset": 10})
    assert body["valid"] is False
    assert body["errors"][0]["code"] == "range_overlap"
    assert "Blocker" in body["errors"][0]["message"]
    assert body["numberChanges"] == [], "no partial arrangement on blockers"


def test_shift_interval_cross_band_and_empty_are_honest():
    doc = doc_of(net_row(895), net_row(896), ch_row(5, cid="ch_00000005"))
    body = organization.plan(
        doc, intent={"type": "shift_interval",
                     "range": {"start": 895, "end": 896}, "offset": 10})
    assert body["errors"][0]["code"] == "cross_band"
    body = organization.plan(
        doc, intent={"type": "shift_interval",
                     "range": {"start": 400, "end": 402}, "offset": 5})
    assert body["valid"] and body["noop"], "empty interval is a no-op"
    body = organization.plan(
        doc, intent={"type": "shift_interval",
                     "range": {"start": 400, "end": 402}, "offset": 0})
    assert body["valid"] and body["noop"]
    body = organization.plan(
        doc, intent={"type": "shift_interval",
                     "range": {"start": 99, "end": 100}, "offset": 5})
    assert body["errors"][0]["code"] == "cross_band"


# ---------------------------------------------------------------------------
# arrange_range (useAvailable + exclusive)
# ---------------------------------------------------------------------------


def performers_range_doc():
    # P* are the selection; Out* are bystanders INSIDE the range; one more
    # selection row (304's P3) keeps the pack non-trivial.
    return doc_of(
        net_row(300, "P1"), net_row(301, "Out1"), net_row(302, "P2"),
        net_row(304, "P3"), net_row(305, "Out2"), net_row(450, "Beyond"))


def test_arrange_range_skips_outsiders_exact_capacity():
    doc = performers_range_doc()
    sel = ["net_0000012c", "net_0000012e", "net_00000130"]  # P1, P2, P3
    body = organization.plan(
        doc, intent={"type": "arrange_range", "channelIds": sel,
                     "range": {"start": 300, "end": 306}})
    assert body["valid"] and not body["noop"]
    got = moves_by_ref(body)
    # the selection vacates first, so it packs into the ascending free
    # positions [300, 302, 303, 304, 306]: P1 and P2 keep their own slots,
    # P3 pulls up next to P2 (301 is an outsider hole, 305 the other).
    assert got["net_00000130"] == (304, 303, "range-pack", True)
    assert "net_0000012c" not in got and "net_0000012e" not in got
    # outsiders stay put — including the one beyond the packed last slot
    assert "net_0000012d" not in got and "net_00000131" not in got
    assert "net_000001c2" not in got
    cap = body["capacity"]
    assert cap["range"]["totalSlots"] == 7
    assert cap["range"]["outsiderSlots"] == 2
    assert cap["shortfall"] == 0


def test_arrange_range_exclusive_relocates_all_outsiders():
    # one outsider sits BEYOND the packed group's single slot: exclusive still
    # relocates it — the whole range empties, then the group packs from 300.
    doc = doc_of(net_row(300, "P1"), net_row(450, "Out"),
                 net_row(701, "Far"), net_row(750, "Holder"))
    body = organization.plan(
        doc, intent={"type": "arrange_range", "channelIds": ["net_0000012c"],
                     "range": {"start": 300, "end": 700},
                     "strategy": "exclusive",
                     "outside": {"start": 701, "end": 899}})
    got = moves_by_ref(body)
    # free outside positions start at 702 (Far holds 701)
    assert got["net_000001c2"] == (450, 702, "exclusive-outside-relocation",
                                   False)
    # P1 packs at 300 where it already is: no entry, nothing else moves
    assert set(got) == {"net_000001c2"}
    assert "net_000002bd" not in got and "net_000002ee" not in got
    assert body["capacity"]["outside"]["freeSlots"] == 197
    assert [op["op"] for op in body["packet"]["ops"]] == ["channels.renumber"]


def test_arrange_range_exclusive_requires_explicit_outside():
    doc = performers_range_doc()
    body = organization.plan(
        doc, intent={"type": "arrange_range", "channelIds": ["net_0000012c"],
                     "range": {"start": 300, "end": 306},
                     "strategy": "exclusive"})
    assert body["errors"][0]["code"] == "bad_range"
    body = organization.plan(
        doc, intent={"type": "arrange_range", "channelIds": ["net_0000012c"],
                     "range": {"start": 300, "end": 306},
                     "strategy": "exclusive",
                     "outside": {"start": 305, "end": 308}})
    assert body["errors"][0]["code"] == "range_overlap"


def test_arrange_range_shortfall_reports_no_partial():
    doc = doc_of(net_row(300, "P1"), net_row(301, "P2"), net_row(302, "Out"),
                 net_row(303, "P3"), net_row(305, "P4"))
    body = organization.plan(
        doc, intent={"type": "arrange_range",
                     "channelIds": ["net_0000012c", "net_0000012d",
                                    "net_0000012f", "net_00000131"],
                     "range": {"start": 300, "end": 303}})
    assert body["valid"] is False
    assert body["errors"][0]["code"] == "no_capacity"
    assert body["capacity"]["shortfall"] == 1
    assert body["numberChanges"] == [] and body["packet"] is None


def test_band_mixed_lists_offenders_never_silent():
    doc = doc_of(net_row(300, "P1"), ch_row(5, "Custom", cid="ch_00000005"))
    body = organization.plan(
        doc, intent={"type": "arrange_range",
                     "channelIds": ["net_0000012c", "ch_00000005"],
                     "range": {"start": 300, "end": 306}})
    assert body["valid"] is False
    assert body["errors"][0]["code"] == "band_mixed"
    assert "ch_00000005" in body["errors"][0]["message"]


def test_arrange_range_name_order():
    doc = doc_of(net_row(300, "zeta"), net_row(301, "alpha"),
                 net_row(302, "alpha"), net_row(305, "mid"))
    body = organization.plan(
        doc, intent={"type": "arrange_range",
                     "channelIds": ["net_0000012c", "net_0000012d",
                                    "net_0000012e"],
                     "range": {"start": 300, "end": 306}, "order": "name"})
    got = moves_by_ref(body)
    # alpha rows pack first (tied by id ascending), zeta lands after
    assert got["net_0000012d"] == (301, 300, "range-pack", True)
    assert got["net_0000012e"] == (302, 301, "range-pack", True)
    assert got["net_0000012c"] == (300, 302, "range-pack", True)
    assert "net_00000131" not in got, "unselected 'mid' never moves"


# ---------------------------------------------------------------------------
# occupancy honesty, relocation, swap, free_number, assign_group
# ---------------------------------------------------------------------------


def test_occupancy_includes_archived_paused_disabled():
    doc = doc_of(net_row(300, "Ghost", archived=True),
                 net_row(301, "Snozer", paused=True),
                 net_row(302, "Off", enabled=False))
    body = organization.plan(
        doc, overlays=[{"tempRef": "temp-1", "kind": "net"}],
        intent={"type": "insert", "tempRef": "temp-1", "number": 300},
        explain=True)
    # archived/paused/disabled rows shift like anyone else — they occupy
    assert len(body["numberChanges"]) == 4
    assert body["choices"]["shiftUp"]["movedCount"] == 3
    warned = {w["code"] for w in body["warnings"]}
    assert {"archived_rows_moved", "paused_rows_moved",
            "disabled_rows_moved"} <= warned
    body = organization.plan(
        doc, intent={"type": "free_number", "kind": "net", "near": 300})
    assert body["suggestions"]["nextHigher"] == 303


def test_relocate_occupant_names_holder_and_detects_noop():
    doc = doc_of(net_row(300, "Alpha"), net_row(301, "Beta"))
    body = organization.plan(
        doc, intent={"type": "relocate_occupant", "channelId": "net_0000012c",
                     "to": 301})
    assert body["errors"][0]["code"] == "destination_occupied"
    assert "Beta" in body["errors"][0]["message"]
    body = organization.plan(
        doc, intent={"type": "relocate_occupant", "channelId": "net_0000012c",
                     "to": 300})
    assert body["valid"] and body["noop"] and body["packet"] is None
    body = organization.plan(
        doc, intent={"type": "relocate_occupant", "channelId": "net_0000012c",
                     "to": 305})
    assert body["valid"] and not body["noop"]
    assert moves_by_ref(body)["net_0000012c"] == (300, 305, "relocate", True)


def test_swap_intent_and_bad_swaps():
    doc = ids300()
    a, b = ids_by_number(doc)[300], ids_by_number(doc)[301]
    body = organization.plan(doc, intent={"type": "swap", "a": a, "b": b})
    got = moves_by_ref(body)
    assert got[a] == (300, 301, "swap", True)
    assert got[b] == (301, 300, "swap", True)
    body = organization.plan(doc, intent={"type": "swap", "a": a, "b": a})
    assert body["errors"][0]["code"] == "bad_swap"
    body = organization.plan(doc, intent={"type": "swap", "a": a,
                                          "b": "ch_00000005"})
    assert body["errors"][0]["code"] == "unknown_channel"


def test_free_number_suggestions():
    doc = doc_of(net_row(300), net_row(301), net_row(302))
    body = organization.plan(
        doc, intent={"type": "free_number", "kind": "net", "near": 301,
                     "count": 4})
    # 300-302 are all taken: nearest free is a 2-tie between 299 and 303,
    # broken toward the lower number
    assert body["suggestions"] == {"nextHigher": 303, "nearest": 299,
                                   "firstFree": 100,
                                   "list": [303, 304, 305, 306]}
    assert body["noop"] and body["packet"] is None
    body = organization.plan(
        doc, intent={"type": "free_number", "kind": "ch", "near": 50},
        explain=True)
    assert body["choices"]["free"]["firstFree"] == 1


def test_assign_group_compiles_group_put_before_move():
    doc = ids300()
    sel = list(ids_by_number(doc).values())
    body = organization.plan(
        doc, intent={"type": "assign_group", "channelIds": sel,
                     "createGroup": {"name": "Performers"}})
    assert body["valid"] and not body["noop"]
    gid = body["groupChanges"][0]["to"]
    assert gid and gid not in {"grp_my", "grp_net"}
    assert all(c["to"] == gid for c in body["groupChanges"])
    ops = body["packet"]["ops"]
    assert [op["op"] for op in ops] == ["group.put", "channels.move"]
    assert ops[0]["group"]["name"] == "Performers"
    assert ops[0]["group"]["position"] == 3  # appended after the two existing
    assert ops[1]["groupId"] == gid
    assert ops[1]["channelIds"] == sorted(sel)
    # determinism of the derived id
    again = organization.plan(
        doc, intent={"type": "assign_group", "channelIds": sel,
                     "createGroup": {"name": "Performers"}})
    assert again["groupChanges"][0]["to"] == gid


def test_assign_group_existing_group_and_conflicts():
    doc = ids300()
    body = organization.plan(
        doc, intent={"type": "assign_group",
                     "channelIds": [ids_by_number(doc)[300]],
                     "groupId": "grp_my"})
    assert body["groupChanges"][0] == {
        "channelId": "net_0000012c", "tempRef": None,
        "from": "grp_net", "to": "grp_my"}
    assert body["packet"]["ops"] == [{"op": "channels.move",
                                      "channelIds": ["net_0000012c"],
                                      "groupId": "grp_my"}]
    body = organization.plan(
        doc, intent={"type": "assign_group", "channelIds": ["net_0000012c"],
                     "createGroup": {"name": "networks"}})
    assert body["errors"][0]["code"] == "duplicate_group"
    body = organization.plan(
        doc, intent={"type": "assign_group", "channelIds": ["net_0000012c"]})
    assert body["errors"][0]["code"] == "bad_intent"
    body = organization.plan(
        doc, intent={"type": "assign_group", "channelIds": ["net_0000012c"],
                     "groupId": "grp_missing", "createGroup": {"name": "X"}})
    assert body["errors"][0]["code"] == "bad_intent"


def test_group_only_change_to_same_group_is_noop():
    doc = ids300()
    body = organization.plan(
        doc, intent={"type": "assign_group", "channelIds": ["net_0000012c"],
                     "groupId": "grp_net"})
    assert body["valid"] and body["noop"] and body["packet"] is None


# ---------------------------------------------------------------------------
# envelope hygiene: stale revision, bad intents, determinism, purity
# ---------------------------------------------------------------------------


def test_stale_revision_short_circuits_with_current_revision():
    doc = ids300()
    body = organization.plan(doc, intent={"type": "swap", "a": "net_0000012c",
                                          "b": "net_0000012d"},
                             expected_revision=11)
    assert body["valid"] is False
    error = body["errors"][0]
    assert error["path"] == "expectedRevision"
    assert error["code"] == "stale_revision"
    assert error["currentRevision"] == 12
    assert len(body["errors"]) == 1
    assert body["numberChanges"] == [] and body["packet"] is None


def test_bad_intents_are_typed():
    doc = ids300()
    body = organization.plan(doc, intent={"type": "teleport"})
    assert body["errors"][0]["code"] == "bad_intent"
    body = organization.plan(doc, intent="swap")
    assert body["errors"][0]["code"] == "bad_intent"
    body = organization.plan(doc, intent={"type": "insert",
                                          "channelId": "net_0000012c",
                                          "tempRef": "temp-1", "number": 300})
    assert body["errors"][0]["code"] == "bad_intent"
    body = organization.plan(doc, intent={"type": "arrange_range",
                                          "channelIds": ["net_0000012c"],
                                          "range": {"start": 99, "end": 100}})
    assert body["errors"][0]["code"] == "cross_band"
    body = organization.plan(doc, overlays=[{"tempRef": "net_0000012c",
                                             "kind": "net"}],
                             intent={"type": "insert",
                                     "tempRef": "net_0000012c",
                                     "number": 305})
    assert body["errors"][0]["code"] == "bad_temp_ref"
    body = organization.plan(doc, overlays=[{"tempRef": "t", "kind": "net"},
                                            {"tempRef": "t", "kind": "net"}],
                             intent={"type": "insert", "tempRef": "t",
                                     "number": 305})
    assert body["errors"][0]["code"] == "duplicate_temp_ref"


def test_planner_is_deterministic_and_pure():
    doc = performers_range_doc()
    frozen = copy.deepcopy(doc)
    intent = {"type": "arrange_range",
              "channelIds": ["net_00000130", "net_0000012c", "net_0000012e"],
              "range": {"start": 300, "end": 306}, "order": "name"}
    first = organization.plan(doc, intent=intent, explain=True)
    second = organization.plan(doc, intent=intent, explain=True)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert doc == frozen, "planning must never mutate the document"
    rebuilt = performers_range_doc()
    rebuilt["channels"].reverse()
    third = organization.plan(rebuilt, intent=intent, explain=True)
    assert json.dumps(third, sort_keys=True) == json.dumps(first, sort_keys=True)


def test_name_order_tiebreak_uses_id_then_temp_token():
    # two same-named rows + one same-named temp row: name order ties, so the
    # token breaks them — id ascending, and the t:-token AFTER every real id,
    # which visibly reorders the higher-numbered row ahead of the temp.
    doc = doc_of(net_row(300, "Same"), net_row(302, "Same"),
                 net_row(304, "Out"))
    body = organization.plan(
        doc, overlays=[{"tempRef": "zz", "kind": "net", "name": "Same"}],
        intent={"type": "arrange_range",
                "channelIds": ["net_0000012c", "net_0000012e"],
                "tempRefs": ["zz"],
                "range": {"start": 300, "end": 306}, "order": "name"})
    got = moves_by_ref(body)
    # pack order: 300-row (id tie-break) keeps 300, 302-row pulls to 301, and
    # the temp row — token t:zz, after all real ids — takes 302
    assert "net_0000012c" not in got
    assert got["net_0000012e"] == (302, 301, "range-pack", True)
    assert got["zz"] == (None, 302, "range-pack", True)
    assert "net_00000130" not in got
