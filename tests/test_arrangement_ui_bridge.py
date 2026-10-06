"""Phase 4a UI-bridge verification: the channel-studio's REAL wire packets
driven through the REAL Python layer, mock-free.

Every fixture below is shaped exactly as ``ui/index.js`` builds it (field
names/casing copied from the code, not from the contract doc):

* preview  — ``previewChannelArrangement`` (ui/index.js:385): one ``Map``
  arg with ``expectedRevision`` (number), ``correlationToken`` ("org-…"),
  ``intent`` as a JSON STRING, ``explain`` flag, and ``channels`` (the
  overlays) as a JSON STRING.
* apply    — ``applyChannelChanges`` (ui/index.js:367) via ``runTask``
  (ui/index.js:307 flattens every task arg to a string): ``mode``,
  ``requestId`` ("req-…"), ``expectedRevision`` as a STRING, ``ops`` as a
  JSON STRING, ``actor`` "channel-studio".

The packets those previews emit are then frozen the way
``freezeArrangementPacket`` (ui/index.js:1199) freezes them — create-draft
merge into skeletons, opt-in ``channel.put`` appends with the agreement
rule — and committed through ``op_apply_channel_changes``, asserting the
final library, the receipt, and the cosmetic invariants.

The two Phase-4a ``xfail`` tripwires (bare-list ``packet`` envelope;
``move_block`` dropping ``tempRefs``) were fixed backend-side in Phase 4b
(decision 0 of analysis/channel-org-2026-10-05/contract-frozen.md) and are
now real passing assertions.
"""
import copy
import json

from justwatch import channel_ops, contract, criteria, library, lineup
from justwatch import refresh
from justwatch.channel_service import presentation_signature

# ---------------------------------------------------------------------------
# Idioms (mirrored from tests/test_channel_ops.py — plain local copies so the
# file stays self-contained)
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, count=7):
        self.count = count
        self.queries = []

    def submit(self, query, variables):
        self.queries.append(variables)
        rows = [
            {"id": str(i), "title": f"T{i}", "date": "2026-01-01",
             "studio": {"name": "S"}, "files": [{"duration": 600.0}]}
            for i in range(min(self.count, 50))]
        return {"findScenes": {"count": self.count, "scenes": rows}}


class Ctx:
    def __init__(self, data_dir, client, args):
        self.data_dir = data_dir
        self.assets_dir = data_dir / "assets"
        self.client = client
        self.args = args


# ---------------------------------------------------------------------------
# Library builder
# ---------------------------------------------------------------------------

GLYPH = contract.GLYPHS[0]


def net_chan(number, name, cid, *, group="grp_net", **over):
    row = {
        "id": cid, "kind": "net", "number": number, "name": name,
        "glyph": GLYPH, "color": "#445566", "groupId": group,
        "sort": "newest", "seed": 1000 + number, "enabled": True,
        "archived": False, "paused": False,
        "source": {"type": "criteria", "studiosAny": [str(10 + number)]},
        "sourceLabel": "", "programming": {"mode": "fixed"},
        "provenance": {"origin": "v4-final-proposal"},
    }
    row.update(over)
    return row


def bridge_doc(*channels, revision=12, library_id="lib_bridge01"):
    return {
        "schemaVersion": library.STORAGE_VERSION,
        "libraryId": library_id,
        "revision": revision,
        "groups": [
            {"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
            {"id": "grp_net", "name": "Networks", "position": 2, "legacySection": None},
        ],
        "channels": sorted(channels, key=lambda c: c["number"]),
        "recentRequests": [],
    }


def save_lib(tmp_path, doc):
    library.save(tmp_path, doc)
    return doc


def by_number(doc):
    return {c["number"]: c for c in doc["channels"]}


def numbers_of(doc):
    return {c["id"]: c["number"] for c in doc["channels"]}


# ---------------------------------------------------------------------------
# The UI's wire helpers, translated 1:1 to Python
# ---------------------------------------------------------------------------


def ui_preview(tmp_path, *, expected_revision, token, intent,
               overlays=None, explain=False):
    """previewChannelArrangement (ui/index.js:385): JSON-string fields on the
    Map arg, exactly as runOp puts them on the wire."""
    args = {
        "expectedRevision": expected_revision,
        "correlationToken": token,
        "intent": json.dumps(intent),
    }
    if explain:
        args["explain"] = True
    if overlays:
        args["channels"] = json.dumps(overlays)
    return channel_ops.op_preview_channel_arrangement(
        Ctx(tmp_path, FakeClient(), args))


def ui_apply(tmp_path, *, expected_revision, request_id, ops, actor="channel-studio"):
    """applyChannelChanges (ui/index.js:367) + runTask (ui/index.js:309):
    every task arg travels as a string; ops is a JSON string."""
    return channel_ops.op_apply_channel_changes(Ctx(tmp_path, FakeClient(), {
        "mode": "ApplyChannelChanges",
        "requestId": request_id,
        "expectedRevision": str(expected_revision),
        "ops": json.dumps(ops),
        "actor": actor,
    }))


def ui_validate(tmp_path, ops):
    """ValidateChannelChanges — the pre-Apply pre-flight the editor runs."""
    return channel_ops.op_validate_channel_changes(Ctx(tmp_path, FakeClient(), {
        "ops": json.dumps(ops)}))


def ui_freeze(resp, drafts_by_temp=None, opt_in_puts=None):
    """freezeArrangementPacket (ui/index.js:1199): the server packet's ops are
    kept verbatim (``resp.packet.ops`` — the object envelope
    ``{expectedRevision, ops}``); the UI (a) merges full pending-create drafts
    into the channel.create skeletons (skeleton kind/number/groupId/name win
    where the skeleton carries them) and (b) appends explicitly opted-in
    channel.put ops whose number is forced to the plan's final number for
    that channel."""
    final_numbers = {}
    for nc in resp.get("numberChanges") or []:
        ref = nc["channelId"] or nc["tempRef"]
        if ref:
            final_numbers[ref] = nc["to"]
    ops = copy.deepcopy((resp["packet"] or {}).get("ops") or [])
    for op in ops:
        if op["op"] == "channel.create" and drafts_by_temp:
            draft = drafts_by_temp.get(op["tempId"])
            if draft:
                full = copy.deepcopy(draft)
                full.pop("id", None)
                full.pop("seed", None)
                full.pop("provenance", None)
                skel = op["channel"] or {}
                merged = dict(full)
                for key in ("kind", "number", "groupId"):
                    if skel.get(key) is not None:
                        merged[key] = skel[key]
                if skel.get("name"):
                    merged["name"] = skel["name"]
                op["channel"] = merged
    for cid, wire in (opt_in_puts or {}).items():
        put = copy.deepcopy(wire)
        if final_numbers.get(cid) is not None:
            put["number"] = final_numbers[cid]  # the agreement rule
        ops.append({"op": "channel.put", "channel": put})
    return ops


# ---------------------------------------------------------------------------
# 1. Create-at-occupied-number conflict sheet → shift-up chosen → commit
# ---------------------------------------------------------------------------


def test_create_at_occupied_number_shift_up_explained_then_committed(tmp_path):
    temp = "temp-ab12cd34"  # newTempId() shape (ui/index.js:435)
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "Net 300", "net_aaaa0001"),
        net_chan(301, "Net 301", "net_aaaa0002"),
        net_chan(302, "Net 302", "net_aaaa0003")))
    intent = {"type": "insert", "tempRef": temp, "number": 300,
              "direction": "up"}
    overlays = [{"tempRef": temp, "kind": "net", "name": "New Wave",
                 "groupId": "grp_net"}]

    # Opening explain:true call — the whole choice matrix in ONE round-trip.
    explained = ui_preview(tmp_path, expected_revision=12, token="org-tok-1",
                           intent=intent, overlays=overlays, explain=True)
    assert explained["valid"] and not explained["noop"]
    assert explained["correlationToken"] == "org-tok-1"
    choices = explained["choices"]
    assert choices is not None
    assert choices["swap"] == {"available": False,
                               "reason": "a new channel has no original slot "
                                         "to swap into"}
    assert choices["shiftUp"] == {"available": True, "firstFree": 303,
                                  "movedCount": 3}
    assert choices["shiftDown"]["available"] is True
    assert choices["free"]["nextHigher"] == 303
    assert choices["relocate"]["available"] is True

    # The owner picks shift-up: the chosen plan is re-previewed (explain off)
    # and ITS packet is frozen.
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-1",
                      intent=intent, overlays=overlays)
    assert plan["choices"] is None
    assert plan["packet"] is not None

    draft = {"kind": "net", "name": "New Wave", "groupId": "grp_net",
             "glyph": GLYPH, "color": "#332211", "sort": "shuffle",
             "source": {"type": "criteria", "tagsAny": ["77"]}}
    ops = ui_freeze(plan, drafts_by_temp={temp: draft})
    assert [op["op"] for op in ops] == ["channel.create", "channels.renumber"]
    created_op = ops[0]
    assert created_op["channel"]["kind"] == "net"
    assert created_op["channel"]["number"] == 300
    assert created_op["channel"]["source"] == draft["source"], \
        "the owner's draft (source/color/glyph) rides the merged skeleton"
    assert "id" not in created_op["channel"] and "seed" not in created_op["channel"]

    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-1", ops=ops)
    assert receipt["status"] == "committed", receipt
    assert receipt["revision"] == 13, "exactly ONE revision for the arrangement"
    new_id = receipt["idMap"][temp]

    doc = library.load(tmp_path)
    rows = by_number(doc)
    assert rows[300]["id"] == new_id
    assert rows[300]["name"] == "New Wave"
    assert rows[300]["groupId"] == "grp_net"
    assert rows[300]["source"] == draft["source"]
    assert rows[301]["id"] == "net_aaaa0001"
    assert rows[302]["id"] == "net_aaaa0002"
    assert rows[303]["id"] == "net_aaaa0003"
    assert receipt["refresh"].get(new_id) == "refreshed", \
        "a creation reindexes inline"
    assert refresh.read_pending(tmp_path) == []


# ---------------------------------------------------------------------------
# 2. Same conflict sheet, shift-down mirror
# ---------------------------------------------------------------------------


def test_create_at_occupied_number_shift_down_mirror(tmp_path):
    temp = "temp-ff11cc22"
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "Net 300", "net_aaaa0001"),
        net_chan(301, "Net 301", "net_aaaa0002")))
    intent = {"type": "insert", "tempRef": temp, "number": 300,
              "direction": "down"}
    overlays = [{"tempRef": temp, "kind": "net", "name": "Second Wave",
                 "groupId": "grp_net"}]
    explained = ui_preview(tmp_path, expected_revision=12, token="org-tok-2",
                           intent=intent, overlays=overlays, explain=True)
    assert explained["choices"]["shiftDown"] == {
        "available": True, "firstFree": 299, "movedCount": 1}

    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-2",
                      intent=intent, overlays=overlays)
    ops = ui_freeze(plan, drafts_by_temp={temp: {
        "kind": "net", "name": "Second Wave", "groupId": "grp_net",
        "glyph": GLYPH, "color": "#332211", "sort": "shuffle",
        "source": {"type": "criteria", "tagsAny": ["78"]}}})
    assert [op["op"] for op in ops] == ["channel.create", "channels.renumber"]
    assert ops[1]["assignments"] == [{"channelId": "net_aaaa0001",
                                      "number": 299}]

    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-2", ops=ops)
    assert receipt["status"] == "committed"
    new_id = receipt["idMap"][temp]
    rows = by_number(library.load(tmp_path))
    assert rows[299]["id"] == "net_aaaa0001", "the 300 row shifted DOWN"
    assert rows[300]["id"] == new_id
    assert rows[301]["id"] == "net_aaaa0002", "301 is untouched"


# ---------------------------------------------------------------------------
# 3. Existing channel into an occupied number with a dirty name — the
#    explicitly opted-in channel.put rides ONE packet (no clean-draft
#    prerequisite)
# ---------------------------------------------------------------------------


def test_dirty_name_opt_in_put_agrees_with_renumber_in_one_packet(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "Alpha", "net_aaaa0001"),
        net_chan(301, "Beta", "net_aaaa0002"),
        net_chan(302, "Gamma", "net_aaaa0003")))
    intent = {"type": "insert", "channelId": "net_aaaa0001", "number": 301,
              "direction": "up"}
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-3",
                      intent=intent)
    assert plan["valid"] and not plan["noop"]
    assert plan["packet"]["ops"] == [{"op": "channels.renumber", "assignments": [
        {"channelId": "net_aaaa0001", "number": 301},
        {"channelId": "net_aaaa0002", "number": 302},
        {"channelId": "net_aaaa0003", "number": 303}]}]

    # The owner renamed Alpha in the editor (dirty draft, never Applied) and
    # opted the channel in. toWireChannel(draft) rides the FULL record.
    dirty = copy.deepcopy(plan and library.load(tmp_path)["channels"][0])
    dirty["name"] = "Alpha Prime"
    stored = {c["id"]: c for c in library.load(tmp_path)["channels"]}
    for key, value in stored["net_aaaa0001"].items():
        dirty.setdefault(key, value)
    ops = ui_freeze(plan, opt_in_puts={"net_aaaa0001": dirty})
    assert ops[-1]["op"] == "channel.put"
    assert ops[-1]["channel"]["number"] == 301, "agreement rule forces the plan's final number"
    assert ops[-1]["channel"]["name"] == "Alpha Prime"

    assert ui_validate(tmp_path, ops)["valid"] is True
    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-3", ops=ops)
    assert receipt["status"] == "committed", receipt
    assert receipt["revision"] == 13

    doc = library.load(tmp_path)
    rows = by_number(doc)
    assert rows[301]["id"] == "net_aaaa0001"
    assert rows[301]["name"] == "Alpha Prime", \
        "the dirty name committed with the arrangement — no clean-draft step"
    assert rows[302]["id"] == "net_aaaa0002" and rows[303]["id"] == "net_aaaa0003"
    assert rows[301]["seed"] == stored["net_aaaa0001"]["seed"]
    assert rows[301]["kind"] == "net"
    assert rows[301]["provenance"] == stored["net_aaaa0001"]["provenance"]
    assert receipt["refresh"] == {}, "a rename + renumber is cosmetic"
    assert refresh.read_pending(tmp_path) == []


# ---------------------------------------------------------------------------
# 4. move_block of 3 selected with bystanders displaced — atomic commit
# ---------------------------------------------------------------------------


def test_move_block_displaced_bystanders_reviewed_and_committed(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(171, "A", "net_aaaa0001"),
        net_chan(173, "B", "net_aaaa0002"),
        net_chan(180, "C", "net_aaaa0003"),
        net_chan(300, "X", "net_bbbb0001"),
        net_chan(301, "Y", "net_bbbb0002"),
        net_chan(302, "Z", "net_bbbb0003")))
    # buildIntent() move_block shape (ui/index.js:2675): channelIds in block
    # order, tempRefs always present (empty here), start.
    intent = {"type": "move_block",
              "channelIds": ["net_aaaa0001", "net_aaaa0002", "net_aaaa0003"],
              "tempRefs": [], "start": 300}
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-4",
                      intent=intent)
    assert plan["valid"] and not plan["noop"]
    assert [c["channelId"] for c in plan["displaced"]] == [
        "net_bbbb0001", "net_bbbb0002", "net_bbbb0003"]
    assert all(c["selected"] is False and c["reason"] == "block-displaced"
               for c in plan["displaced"])
    assert plan["packet"]["ops"] == [{"op": "channels.renumber", "assignments": [
        {"channelId": "net_aaaa0001", "number": 300},
        {"channelId": "net_aaaa0002", "number": 301},
        {"channelId": "net_aaaa0003", "number": 302},
        {"channelId": "net_bbbb0001", "number": 303},
        {"channelId": "net_bbbb0002", "number": 304},
        {"channelId": "net_bbbb0003", "number": 305}]}]

    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-4", ops=ui_freeze(plan))
    assert receipt["status"] == "committed"
    assert receipt["touched"] == 1, "one renumber op, committed atomically"
    assert numbers_of(library.load(tmp_path)) == {
        "net_aaaa0001": 300, "net_aaaa0002": 301, "net_aaaa0003": 302,
        "net_bbbb0001": 303, "net_bbbb0002": 304, "net_bbbb0003": 305}
    assert receipt["refresh"] == {}


# ---------------------------------------------------------------------------
# 5. arrange_range useAvailable — outsiders keep their numbers, exact capacity
# ---------------------------------------------------------------------------


def test_arrange_range_use_available_packs_selection_outsiders_stay(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "P1", "net_bbbb0001"),
        net_chan(301, "Out1", "net_bbbb0002"),
        net_chan(302, "P2", "net_bbbb0003"),
        net_chan(304, "P3", "net_bbbb0004"),
        net_chan(305, "Out2", "net_bbbb0005"),
        net_chan(450, "Beyond", "net_bbbb0006")))
    intent = {"type": "arrange_range",
              "channelIds": ["net_bbbb0001", "net_bbbb0003", "net_bbbb0004"],
              "tempRefs": [],
              "range": {"start": 300, "end": 306},
              "order": "number", "strategy": "useAvailable"}
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-5",
                      intent=intent)
    assert plan["valid"] and not plan["noop"]
    cap = plan["capacity"]
    assert cap["range"]["start"] == 300 and cap["range"]["end"] == 306
    assert cap["range"]["totalSlots"] == 7
    assert cap["range"]["outsiderSlots"] == 2
    assert cap["neededSlots"] == 3 and cap["shortfall"] == 0
    changes = {(c["channelId"] or c["tempRef"]): c for c in plan["numberChanges"]}
    assert set(changes) == {"net_bbbb0004"}, "P3 is the only row that moves"
    assert changes["net_bbbb0004"]["to"] == 303
    assert changes["net_bbbb0004"]["reason"] == "range-pack"

    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-5", ops=ui_freeze(plan))
    assert receipt["status"] == "committed"
    numbers = numbers_of(library.load(tmp_path))
    assert numbers["net_bbbb0004"] == 303
    assert numbers["net_bbbb0001"] == 300 and numbers["net_bbbb0003"] == 302
    assert numbers["net_bbbb0002"] == 301 and numbers["net_bbbb0005"] == 305
    assert numbers["net_bbbb0006"] == 450, "the outsider beyond the range stays"


# ---------------------------------------------------------------------------
# 6. arrange_range exclusive with an explicit outside interval — one-time,
#    nothing stored about it
# ---------------------------------------------------------------------------


def test_arrange_range_exclusive_relocates_outsiders_stores_nothing(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "P1", "net_bbbb0001"),
        net_chan(450, "Out", "net_bbbb0002"),
        net_chan(701, "Far", "net_bbbb0003"),
        net_chan(750, "Holder", "net_bbbb0004")))
    before_groups = copy.deepcopy(library.load(tmp_path)["groups"])
    intent = {"type": "arrange_range", "channelIds": ["net_bbbb0001"],
              "tempRefs": [], "range": {"start": 300, "end": 700},
              "order": "number", "strategy": "exclusive",
              "outside": {"start": 701, "end": 899}}
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-6",
                      intent=intent)
    assert plan["valid"] and not plan["noop"]
    assert plan["capacity"]["outside"]["freeSlots"] == 197
    assert plan["packet"]["ops"] == [{"op": "channels.renumber", "assignments": [
        {"channelId": "net_bbbb0002", "number": 702}]}]

    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-6", ops=ui_freeze(plan))
    assert receipt["status"] == "committed"
    assert receipt["revision"] == 13
    doc = library.load(tmp_path)
    numbers = numbers_of(doc)
    assert numbers["net_bbbb0002"] == 702 and numbers["net_bbbb0001"] == 300
    assert numbers["net_bbbb0003"] == 701
    # The exclusive arrangement is ONE-TIME: no reservation field anywhere.
    assert doc["groups"] == before_groups
    for group in doc["groups"]:
        assert set(group) == {"id", "name", "position", "legacySection"}
    for channel in doc["channels"]:
        assert not any("reserv" in k.lower() or "arrange" in k.lower()
                       for k in channel)


# ---------------------------------------------------------------------------
# 7. assign_group with createGroup {name} only, concatenated with a
#    same-revision arrange_range packet (the UI's combineArrangementResponses)
# ---------------------------------------------------------------------------


def test_create_group_plus_range_packets_concatenate_into_one_commit(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "N1", "net_cccc0001"),
        net_chan(301, "N2", "net_cccc0002"),
        net_chan(302, "N3", "net_cccc0003"),
        net_chan(303, "N4", "net_cccc0004")))
    assign_intent = {"type": "assign_group",
                     "channelIds": ["net_cccc0001", "net_cccc0002",
                                    "net_cccc0003", "net_cccc0004"],
                     "tempRefs": [], "createGroup": {"name": "Performers"}}
    range_intent = {"type": "arrange_range",
                    "channelIds": ["net_cccc0001", "net_cccc0002",
                                   "net_cccc0003", "net_cccc0004"],
                    "tempRefs": [], "range": {"start": 400, "end": 403},
                    "order": "number", "strategy": "useAvailable"}
    assign = ui_preview(tmp_path, expected_revision=12, token="org-tok-7",
                        intent=assign_intent)
    rng = ui_preview(tmp_path, expected_revision=12, token="org-tok-7",
                     intent=range_intent)
    assert assign["valid"] and rng["valid"]
    gid = assign["groupChanges"][0]["to"]
    assert gid.startswith("grp_"), "the derived group id flows through"

    # combineArrangementResponses (ui/index.js:1247): group ops first, then
    # creates, then moves, then renumbers.
    combined = assign["packet"]["ops"] + rng["packet"]["ops"]
    assert [op["op"] for op in combined] == ["group.put", "channels.move",
                                             "channels.renumber"]
    assert combined[0]["group"] == {"id": gid, "name": "Performers",
                                    "position": 3}
    assert combined[1]["groupId"] == gid
    assert len(combined[2]["assignments"]) == 4

    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-7", ops=combined)
    assert receipt["status"] == "committed"
    assert receipt["revision"] == 13, "two same-revision packets, ONE revision"
    doc = library.load(tmp_path)
    group = next(g for g in doc["groups"] if g["id"] == gid)
    assert group["name"] == "Performers"
    assert numbers_of(doc) == {
        "net_cccc0001": 400, "net_cccc0002": 401,
        "net_cccc0003": 402, "net_cccc0004": 403}
    assert all(c["groupId"] == gid for c in doc["channels"]
               if c["id"].startswith("net_cccc"))
    assert receipt["refresh"] == {}, "group + number moves are cosmetic"


# ---------------------------------------------------------------------------
# 8. shift_interval into an untouched blocker → typed error; the corrected
#    interval commits with gaps preserved
# ---------------------------------------------------------------------------


def test_shift_interval_blocker_typed_error_then_corrected_range(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(200, "Two Hundred", "net_dddd0001"),
        net_chan(202, "Two Oh Two", "net_dddd0002"),
        net_chan(205, "Outside", "net_dddd0003"),
        net_chan(210, "Blocker", "net_dddd0004")))
    intent = {"type": "shift_interval",
              "range": {"start": 200, "end": 202}, "offset": 10}
    blocked = ui_preview(tmp_path, expected_revision=12, token="org-tok-8",
                         intent=intent)
    assert blocked["valid"] is False
    assert blocked["errors"][0]["code"] == "range_overlap"
    assert "Blocker" in blocked["errors"][0]["message"]
    assert blocked["packet"] is None, "an invalid plan offers nothing to stage"

    corrected_doc = bridge_doc(
        net_chan(200, "Two Hundred", "net_dddd0001"),
        net_chan(202, "Two Oh Two", "net_dddd0002"),
        net_chan(205, "Outside", "net_dddd0003"))
    save_lib(tmp_path, corrected_doc)
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-8",
                      intent=intent)
    assert plan["valid"] and not plan["noop"]
    assert plan["packet"]["ops"] == [{"op": "channels.renumber", "assignments": [
        {"channelId": "net_dddd0001", "number": 210},
        {"channelId": "net_dddd0002", "number": 212}]}]

    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-8", ops=ui_freeze(plan))
    assert receipt["status"] == "committed"
    numbers = numbers_of(library.load(tmp_path))
    assert numbers["net_dddd0001"] == 210 and numbers["net_dddd0002"] == 212
    assert numbers["net_dddd0003"] == 205
    taken = set(numbers.values())
    assert {n for n in range(200, 213) if n not in taken} == \
        {200, 201, 202, 203, 204, 206, 207, 208, 209, 211}, \
        "the shift preserves the gaps (the vacated 200/202 stay free)"


# ---------------------------------------------------------------------------
# 9. Revision conflict between preview and Apply → frozen packet retained,
#    replan as a NEW requestId succeeds
# ---------------------------------------------------------------------------


def test_revision_conflict_retains_staged_packet_then_replan_commits(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "Alpha", "net_aaaa0001"),
        net_chan(301, "Beta", "net_aaaa0002"),
        net_chan(302, "Gamma", "net_aaaa0003")))
    intent = {"type": "insert", "channelId": "net_aaaa0002", "number": 300,
              "direction": "up"}
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-9",
                      intent=intent)
    assert plan["valid"]
    frozen = {"baseRevision": 12, "correlationToken": "org-tok-9",
              "packet": {"expectedRevision": 12, "requestId": "req-bridge-9",
                         "ops": ui_freeze(plan)}}

    # A concurrent writer bumps the library between preview and Apply.
    concurrent = copy.deepcopy(library.load(tmp_path))
    gamma = next(c for c in concurrent["channels"] if c["id"] == "net_aaaa0003")
    gamma["name"] = "Gamma Renamed"
    concurrent_ctx = Ctx(tmp_path, FakeClient(), {
        "mode": "ApplyChannelChanges", "requestId": "req-other-9",
        "expectedRevision": "12",
        "ops": json.dumps([{"op": "channel.put", "channel": gamma}])})
    assert channel_ops.op_apply_channel_changes(concurrent_ctx)["status"] == \
        "committed"

    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-9", ops=frozen["packet"]["ops"])
    assert receipt["status"] == "rejected"
    assert receipt["error"] == "revision_conflict"
    assert receipt["currentRevision"] == 13
    # The UI keeps the draft + frozen packet staged (org store untouched).
    assert frozen["packet"]["ops"] == ui_freeze(plan)

    # Replan against the NEW revision: a NEW preview and a NEW requestId.
    # (A cosmetic concurrent rename cannot change the deterministic number
    # plan — the packet may replay byte-identically; what makes it safe is
    # the revalidation against r13 plus the fresh request identity.)
    replan = ui_preview(tmp_path, expected_revision=13, token="org-tok-9b",
                        intent=intent)
    assert replan["valid"] and replan["revision"] == 13
    assert replan["packet"] is not None
    receipt2 = ui_apply(tmp_path, expected_revision=13,
                        request_id="req-bridge-9b", ops=ui_freeze(replan))
    assert receipt2["status"] == "committed"
    assert receipt2["revision"] == 14

    # The dead requestId replays its REJECTED receipt byte-identically.
    again = ui_apply(tmp_path, expected_revision=12,
                     request_id="req-bridge-9", ops=frozen["packet"]["ops"])
    assert again["status"] == "rejected"
    assert again["error"] == "revision_conflict"
    assert library.load(tmp_path)["revision"] == 14


# ---------------------------------------------------------------------------
# 10. Lost response → same requestId replays the committed receipt
# ---------------------------------------------------------------------------


def test_lost_response_replays_receipt_without_second_commit(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "Alpha", "net_aaaa0001"),
        net_chan(301, "Beta", "net_aaaa0002")))
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-10",
                      intent={"type": "move_block", "channelIds": [
                          "net_aaaa0001", "net_aaaa0002"],
                          "tempRefs": [], "start": 400})
    ops = ui_freeze(plan)
    first = ui_apply(tmp_path, expected_revision=12,
                     request_id="req-bridge-10", ops=ops)
    assert first["status"] == "committed" and first["revision"] == 13

    # The response was lost; the coordinator resubmits byte-identical args.
    replay = ui_apply(tmp_path, expected_revision=12,
                      request_id="req-bridge-10", ops=ops)
    assert replay["status"] == "committed"
    assert replay["revision"] == first["revision"]
    assert replay.get("idMap") == first.get("idMap")
    assert replay.get("digest") == first.get("digest")
    # Nuance: the replay returns the STORED durable receipt, but the task op
    # re-annotates it (refresh/note) after a no-op journal check — harmless,
    # nothing is re-indexed and the revision never moves.
    assert replay["refresh"] == {}
    assert replay["note"] == first["note"]
    doc = library.load(tmp_path)
    assert doc["revision"] == 13, "the revision advanced exactly once"
    assert numbers_of(doc) == {"net_aaaa0001": 400, "net_aaaa0002": 401}
    # GetChannelApplyResult resolves the same durable receipt.
    lookup = channel_ops.op_get_channel_apply_result(Ctx(
        tmp_path, FakeClient(), {"requestId": "req-bridge-10"}))
    assert lookup["status"] == "committed" and lookup["revision"] == 13


# ---------------------------------------------------------------------------
# 11. Cosmetic invariants through the UI-shaped packet path
# ---------------------------------------------------------------------------


def test_ui_shaped_arrangement_packet_is_fully_cosmetic(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(300, "Alpha", "net_aaaa0001"),
        net_chan(301, "Beta", "net_aaaa0002"),
        net_chan(302, "Gamma", "net_aaaa0003")))
    pub = tmp_path / "programming" / "net_aaaa0003.json"
    pub.parent.mkdir()
    pub.write_text('{"published": true}', encoding="utf-8")
    before = {c["id"]: {
        "rotation": lineup.rotation_version(c["source"], c["sort"], c["seed"],
                                            contract.ROTATION_SIZE),
        "membership": criteria.source_signature(c["source"]),
        "presentation": presentation_signature(c),
        "seed": c["seed"],
    } for c in library.load(tmp_path)["channels"]}

    # The UI-shaped flow: preview insert of the 300 row at the occupied 302
    # (direction up), freeze its packet, apply it.
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-11",
                      intent={"type": "insert", "channelId": "net_aaaa0001",
                              "number": 302, "direction": "up"})
    assert plan["valid"] and not plan["noop"]
    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-11", ops=ui_freeze(plan))
    assert receipt["status"] == "committed"
    assert receipt["refresh"] == {}, "a renumber never reindexes"
    assert refresh.read_pending(tmp_path) == [], "the journal stays empty"

    after_rows = {c["id"]: c for c in library.load(tmp_path)["channels"]}
    for cid, was in before.items():
        channel = after_rows[cid]
        assert lineup.rotation_version(channel["source"], channel["sort"],
                                       channel["seed"],
                                       contract.ROTATION_SIZE) == was["rotation"]
        assert criteria.source_signature(channel["source"]) == was["membership"]
        assert channel["seed"] == was["seed"]
    assert numbers_of(library.load(tmp_path)) == {
        "net_aaaa0001": 302, "net_aaaa0002": 301, "net_aaaa0003": 303}
    touched = {"net_aaaa0001", "net_aaaa0003"}
    for cid in touched:
        assert presentation_signature(after_rows[cid]) != before[cid]["presentation"]
    assert presentation_signature(after_rows["net_aaaa0002"]) == \
        before["net_aaaa0002"]["presentation"]
    assert pub.read_text(encoding="utf-8") == '{"published": true}', \
        "publications are keyed by id and never rewritten"


# ---------------------------------------------------------------------------
# 12. Undo / reversal: the inverse map applies as a fresh validated Apply
# ---------------------------------------------------------------------------


def test_stage_reversal_restores_numbers_and_keeps_unrelated_edits(tmp_path):
    save_lib(tmp_path, bridge_doc(
        net_chan(171, "A", "net_aaaa0001"),
        net_chan(173, "B", "net_aaaa0002"),
        net_chan(180, "C", "net_aaaa0003"),
        net_chan(300, "X", "net_bbbb0001"),
        net_chan(301, "Y", "net_bbbb0002"),
        net_chan(302, "Z", "net_bbbb0003")))
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-12",
                      intent={"type": "move_block",
                              "channelIds": ["net_aaaa0001", "net_aaaa0002",
                                             "net_aaaa0003"],
                              "tempRefs": [], "start": 300})
    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-12a", ops=ui_freeze(plan))
    assert receipt["status"] == "committed" and receipt["revision"] == 13

    # An unrelated SOURCE edit lands after the arrangement (a different channel).
    doc = library.load(tmp_path)
    x_row = next(c for c in doc["channels"] if c["id"] == "net_bbbb0001")
    edited = copy.deepcopy(x_row)
    edited["source"] = {"type": "criteria", "tagsAny": ["88"]}
    ctx = Ctx(tmp_path, FakeClient(), {
        "mode": "ApplyChannelChanges", "requestId": "req-bridge-12b",
        "expectedRevision": "13",
        "ops": json.dumps([{"op": "channel.put", "channel": edited}])})
    assert channel_ops.op_apply_channel_changes(ctx)["status"] == "committed"

    # buildReversal (ui/index.js:1290): the inverse number map over existing
    # channels only, guarded by "still at its arranged number".
    staged_before_after = {c["channelId"] or c["tempRef"]:
                           [c["from"], c["to"]]
                           for c in plan["numberChanges"]}
    current = numbers_of(library.load(tmp_path))
    assignments = []
    for ref, (frm, to) in staged_before_after.items():
        if current[ref] == to:
            assignments.append({"channelId": ref, "number": frm})
    inverse_ops = [{"op": "channels.renumber", "assignments": assignments}]

    check = ui_validate(tmp_path, inverse_ops)
    assert check["valid"] is True, check["errors"]
    receipt2 = ui_apply(tmp_path, expected_revision=14,
                        request_id="req-bridge-12c", ops=inverse_ops)
    assert receipt2["status"] == "committed"
    assert receipt2["revision"] == 15

    doc = library.load(tmp_path)
    assert numbers_of(doc) == {
        "net_aaaa0001": 171, "net_aaaa0002": 173, "net_aaaa0003": 180,
        "net_bbbb0001": 300, "net_bbbb0002": 301, "net_bbbb0003": 302}
    x_row = next(c for c in doc["channels"] if c["id"] == "net_bbbb0001")
    assert x_row["source"] == {"type": "criteria", "tagsAny": ["88"]}, \
        "the reversal renumbers; it never reverts unrelated source edits"


# ---------------------------------------------------------------------------
# Former xfail tripwires (UI↔backend wire mismatches found in the Phase-4a
# bridge pass), fixed backend-side in Phase 4b per decision 0 of
# analysis/channel-org-2026-10-05/contract-frozen.md — now real assertions.
# ---------------------------------------------------------------------------


def test_preview_packet_is_the_object_the_ui_freezes(tmp_path):
    save_lib(tmp_path, bridge_doc(net_chan(300, "Alpha", "net_aaaa0001")))
    plan = ui_preview(tmp_path, expected_revision=12, token="org-tok-x1",
                      intent={"type": "relocate_occupant",
                              "channelId": "net_aaaa0001", "to": 305})
    assert plan["valid"] and not plan["noop"]
    assert isinstance(plan["packet"], dict), "the UI freezes packet.ops"
    assert isinstance(plan["packet"]["ops"], list)
    assert plan["packet"]["expectedRevision"] == 12
    assert plan["packet"]["ops"] == [{"op": "channels.renumber", "assignments": [
        {"channelId": "net_aaaa0001", "number": 305}]}]


def test_move_block_with_draft_rows_never_drops_them_silently(tmp_path):
    save_lib(tmp_path, bridge_doc(net_chan(300, "X", "net_bbbb0001")))
    plan = ui_preview(
        tmp_path, expected_revision=12, token="org-tok-x2",
        intent={"type": "move_block", "channelIds": ["net_bbbb0001"],
                "tempRefs": ["temp-ab12cd34"], "start": 300},
        overlays=[{"tempRef": "temp-ab12cd34", "kind": "net",
                   "name": "Draft", "groupId": "grp_net"}])
    assert plan["valid"] and not plan["noop"]
    # the temp row takes the next block slot and is reported, never dropped
    assert plan["numberChanges"] == [
        {"channelId": None, "tempRef": "temp-ab12cd34", "from": None,
         "to": 301, "selected": True, "reason": "direct"}]
    assert plan["created"] == [{"tempRef": "temp-ab12cd34", "number": 301,
                                "groupId": "grp_net"}]
    # the packet skeleton carries the planned number
    assert plan["packet"]["expectedRevision"] == 12
    assert plan["packet"]["ops"] == [
        {"op": "channel.create", "tempId": "temp-ab12cd34",
         "channel": {"kind": "net", "number": 301, "name": "Draft",
                     "groupId": "grp_net"}}]

    # end-to-end: the frozen packet merges the owner's draft and commits.
    ops = ui_freeze(plan, drafts_by_temp={"temp-ab12cd34": {
        "kind": "net", "name": "Draft", "groupId": "grp_net",
        "glyph": GLYPH, "color": "#332211", "sort": "shuffle",
        "source": {"type": "criteria", "tagsAny": ["79"]}}})
    assert ops == [{"op": "channel.create", "tempId": "temp-ab12cd34",
                    "channel": {"kind": "net", "name": "Draft",
                                "groupId": "grp_net", "glyph": GLYPH,
                                "color": "#332211", "sort": "shuffle",
                                "source": {"type": "criteria",
                                           "tagsAny": ["79"]},
                                "number": 301}}]
    receipt = ui_apply(tmp_path, expected_revision=12,
                       request_id="req-bridge-x2", ops=ops)
    assert receipt["status"] == "committed", receipt
    assert receipt["revision"] == 13
    new_id = receipt["idMap"]["temp-ab12cd34"]
    rows = by_number(library.load(tmp_path))
    assert rows[300]["id"] == "net_bbbb0001"
    assert rows[301]["id"] == new_id and rows[301]["name"] == "Draft"
    assert receipt["refresh"].get(new_id) == "refreshed"
