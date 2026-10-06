"""Rule-semantics tests for the criteria model and channel-library ops.

Rules: ANY vs ALL storage fields, AND between rows, exclusion projection,
descendant depth, range boundaries, text query, UTC recency, signatures.
Ops: paging, touched-record isolation, receipts, refresh journaling, stale
worker rejection — all against a scripted fake Stash client.
"""
import json

import pytest

from justwatch import channel_ops, criteria, refresh
from justwatch import library as library_mod
from justwatch.lineup import created_cutoff


# ---------------------------------------------------------------------------
# criteria: storage fields + projection
# ---------------------------------------------------------------------------


def test_any_and_all_store_in_distinct_fields():
    s = {"type": "criteria", "performersAny": ["1", "2"]}
    f = criteria.build_scene_filter(s)
    assert f["performers"] == {"value": ["1", "2"], "modifier": "INCLUDES"}
    s = {"type": "criteria", "performers": ["1", "2"]}
    f = criteria.build_scene_filter(s)
    assert f["performers"]["modifier"] == "INCLUDES_ALL"


def test_rows_combine_with_and_in_projection():
    s = {"type": "criteria", "tagsAll": None, "tags": ["9"], "studiosAny": ["4"],
         "duration": {"min": 600}, "date": {"from": "2020-01-01", "to": "2025-12-31"}}
    f = criteria.build_scene_filter(s)
    assert f["tags"]["value"] == ["9"]
    assert f["studios"]["modifier"] == "INCLUDES"
    assert f["duration"] == {"value": 600, "value2": 2147483647, "modifier": "BETWEEN"}
    assert f["date"]["modifier"] == "BETWEEN"


def test_exclusions_ride_their_criterion():
    s = {"type": "criteria", "tags": ["9"], "excludeTags": ["9320"],
         "performersAny": ["7"], "excludePerformers": ["13"],
         "studiosAny": ["2"], "excludeStudios": ["5"]}
    f = criteria.build_scene_filter(s)
    assert f["tags"]["excludes"] == ["9320"]
    assert f["performers"]["excludes"] == ["13"]
    assert f["studios"]["excludes"] == ["5"]
    # hierarchy: tags + studios keep descendant inclusion
    assert f["tags"]["depth"] == -1
    assert f["studios"]["depth"] == -1
    assert "depth" not in f["performers"]


def test_exclude_only_pool_is_the_historical_everything_except_shape():
    s = {"type": "criteria", "excludeTags": ["9320"]}
    assert criteria.validate(s) == []
    f = criteria.build_scene_filter(s)
    assert f["tags"] == {"value": [], "modifier": "INCLUDES", "depth": -1,
                         "excludes": ["9320"]}


def test_conflicting_any_all_is_rejected_but_tolerant_projection_prefers_all():
    result = criteria.validate({"type": "criteria", "tags": ["1"], "tagsAny": ["2"]})
    assert result[0]["code"] == "conflicting_rows"
    # projection stays tolerant for legacy reads: ALL wins
    f = criteria.build_scene_filter({"type": "filter", "tags": ["1"], "tagsAny": ["2"]})
    assert f["tags"]["value"] == ["1"]
    assert f["tags"]["modifier"] == "INCLUDES_ALL"


def test_no_rules_at_all_is_invalid_but_empty_results_are_valid():
    assert criteria.validate({"type": "criteria"})[0]["code"] == "empty_rules"
    # a rule that matches nothing is the author's business
    assert criteria.validate({"type": "criteria", "tags": ["99999999"]}) == []


def test_recency_is_utc_and_moves_with_the_calendar():
    # One clock drives BOTH paths (audit E9): the test pins a day instead of
    # racing the real calendar, and the rollover is checked deterministically.
    import datetime as dt
    day = dt.date(2026, 9, 23)
    cutoff = created_cutoff(180, today=day)
    assert cutoff == "2026-03-27"
    f = criteria.build_scene_filter({"type": "criteria", "tags": ["1"], "createdAt": {"withinDays": 180}}, today=day)
    assert f["created_at"]["value"] == cutoff
    assert f["created_at"]["modifier"] == "GREATER_THAN"
    nxt = criteria.build_scene_filter({"type": "criteria", "tags": ["1"], "createdAt": {"withinDays": 180}},
                                      today=day + dt.timedelta(days=1))
    assert nxt["created_at"]["value"] == "2026-03-28"


def test_metadata_only_rule_is_valid():
    s = {"type": "criteria", "duration": {"min": 3600}}
    assert criteria.validate(s) == []
    assert "duration" in criteria.build_scene_filter(s)


def test_signature_ignores_cosmetic_noise_and_list_order():
    a = {"type": "criteria", "performersAny": ["3", "1", "2"], "q": "x"}
    b = {"type": "criteria", "performersAny": ["1", "2", "3"], "q": "x"}
    assert criteria.source_signature(a) == criteria.source_signature(b)
    c = {"type": "criteria", "performersAny": ["1", "2", "3"]}
    assert criteria.source_signature(a) != criteria.source_signature(c)


def test_legacy_custom_source_roundtrips_unchanged():
    legacy = {"type": "tag", "id": "5", "ids": ["5", "7"]}
    assert criteria.normalize(legacy) == legacy
    assert criteria.source_signature(legacy) == criteria.source_signature(
        criteria.normalize(legacy))


# ---------------------------------------------------------------------------
# channel ops against a scripted fake client
# ---------------------------------------------------------------------------


class FakeClient:
    """Counts queries; returns a fixed count + page for findScenes."""

    def __init__(self, count=7, rows=None):
        self.count = count
        self.rows = rows or []
        self.queries = []

    def submit(self, query, variables):
        self.queries.append(variables)
        rows = self.rows or [
            {"id": str(i), "title": f"T{i}", "date": "2026-01-01",
             "studio": {"name": "S"}, "files": [{"duration": 600.0}]}
            for i in range(min(self.count, 50))]
        return {"findScenes": {"count": self.count, "scenes": rows}}


def op_library(tmp_path):
    doc = {
        "schemaVersion": library_mod.STORAGE_VERSION,
        "libraryId": "lib_ops00000",
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
        ],
        "recentRequests": [],
    }
    library_mod.save(tmp_path, doc)
    return doc


class Ctx:
    def __init__(self, data_dir, client, args):
        self.data_dir = data_dir
        self.assets_dir = data_dir / "assets"
        self.client = client
        self.args = args


def test_get_channel_library_pagination_and_search(tmp_path):
    op_library(tmp_path)
    ctx = Ctx(tmp_path, FakeClient(), {"limit": 10})
    full = channel_ops.op_get_channel_library(ctx)
    assert full["total"] == 1 and full["channels"][0]["id"] == "ch_11111111"
    # a lightweight row: no raw source object rides the list
    assert "source" not in full["channels"][0]
    ctx = Ctx(tmp_path, FakeClient(), {"query": "zzz"})
    assert channel_ops.op_get_channel_library(ctx)["total"] == 0


def test_apply_isolation_one_channel_apply_never_publishes_another(tmp_path):
    """Applying channel A's metadata must not touch B even when B 'has a
    draft' elsewhere — the transaction contains exactly the submitted ops."""
    doc = op_library(tmp_path)
    doc["channels"].append({**doc["channels"][0], "id": "ch_22222222", "number": 2,
                            "name": "Two", "seed": 9})
    library_mod.save(tmp_path, doc)
    ctx = Ctx(tmp_path, FakeClient(), {})
    ctx.args = {"requestId": "apply-a", "expectedRevision": 1, "ops": [
        {"op": "channel.put", "channel": {**doc["channels"][0], "name": "One New"}}]}
    receipt = channel_ops.op_apply_channel_changes(ctx)
    assert receipt["status"] == "committed"
    names = {c["id"]: c["name"] for c in library_mod.load(tmp_path)["channels"]}
    assert names == {"ch_11111111": "One New", "ch_22222222": "Two"}


def test_metadata_apply_does_not_enqueue_reindex_content_apply_does(tmp_path):
    doc = op_library(tmp_path)
    ctx = Ctx(tmp_path, FakeClient(), {})
    # rename only
    ctx.args = {"requestId": "meta", "expectedRevision": 1, "ops": [
        {"op": "channel.put", "channel": {**doc["channels"][0], "name": "Renamed"}}]}
    receipt = channel_ops.op_apply_channel_changes(ctx)
    assert receipt["status"] == "committed"
    assert receipt["refresh"] == {}, "cosmetic edits must not reindex"
    assert refresh.read_pending(tmp_path) == []
    # rule change: exactly one channel's membership is refreshed inline
    ctx.args = {"requestId": "pool", "expectedRevision": 2, "ops": [
        {"op": "channel.put", "channel": {**doc["channels"][0], "name": "Renamed",
        "source": {"type": "criteria", "tagsAny": ["8"]}}}]}
    receipt = channel_ops.op_apply_channel_changes(ctx)
    assert receipt["status"] == "committed"
    assert receipt["refresh"] == {"ch_11111111": "refreshed"}
    assert refresh.read_pending(tmp_path) == [], "the inline refresh drained the journal"


def test_stale_worker_publication_is_requeued_against_current_signature(tmp_path):
    op_library(tmp_path)
    refresh.enqueue(tmp_path, ["ch_11111111"], 1, {"ch_11111111": "stale-sig"})
    client = FakeClient()
    result = refresh.process_pending(client, tmp_path, tmp_path / "assets")
    assert result["processed"]["ch_11111111"] == "superseded_requeued"
    pending = refresh.read_pending(tmp_path)
    assert pending[0]["signature"] != "stale-sig"
    assert pending[0]["revision"] == 1


def test_lost_response_resolves_via_receipt(tmp_path):
    doc = op_library(tmp_path)
    ctx = Ctx(tmp_path, FakeClient(), {})
    ctx.args = {"requestId": "lost-1", "expectedRevision": 1, "ops": [
        {"op": "channels.patch", "channelIds": ["ch_11111111"], "patch": {"paused": True}}]}
    first = channel_ops.op_apply_channel_changes(ctx)
    assert first["status"] == "committed"
    # the client that lost the response asks for the receipt by id
    lookup = channel_ops.op_get_channel_apply_result(Ctx(tmp_path, FakeClient(), {"requestId": "lost-1"}))
    assert lookup["status"] == "committed"
    assert lookup["revision"] == first["revision"]
    unknown = channel_ops.op_get_channel_apply_result(Ctx(tmp_path, FakeClient(), {"requestId": "nope"}))
    assert unknown["status"] == "unknown"


def test_preview_is_signature_correlated_and_bounded(tmp_path):
    op_library(tmp_path)
    client = FakeClient(count=120)
    ctx = Ctx(tmp_path, client, {"source": json.dumps(
        {"type": "criteria", "tagsAny": ["3", "4"]}), "sampleLimit": 5})
    result = channel_ops.op_preview_channel_pool(ctx)
    assert result["poolCount"] == 120
    assert result["rotationSize"] == 50  # the bounded on-air loop, not the pool
    assert len(result["sample"]) == 5
    assert len(client.queries) == 1, "preview is one bounded query, not an index"
    assert result["signature"] == criteria.source_signature({"type": "criteria", "tagsAny": ["3", "4"]})


def test_preview_reports_missing_saved_search_without_raising(tmp_path):
    op_library(tmp_path)

    class Missing:
        def submit(self, query, variables):
            raise AssertionError("must not query")

        def submit_saved(self):
            pass

    # resolve_saved_criteria uses client.submit; simulate a missing filter
    class MissingClient:
        def submit(self, query, variables):
            return {"findSavedFilter": None}

    ctx = Ctx(tmp_path, MissingClient(), {"source": {"type": "savedFilter", "id": "404"}})
    result = channel_ops.op_preview_channel_pool(ctx)
    assert result["status"] == "missing_source"


def test_dynamic_scene_count_rows_project_to_nested_filters():
    f = criteria.build_scene_filter({"type": "criteria", "studioSceneCount": {"max": 2}})
    assert f["studios_filter"] == {"scene_count": {"value": 2, "value2": None,
                                                   "modifier": "LESS_THAN"}}
    f = criteria.build_scene_filter({"type": "criteria", "performerSceneCount": {"min": 100}})
    assert f["performers_filter"]["scene_count"]["modifier"] == "BETWEEN"
    assert f["performers_filter"]["scene_count"]["value"] == 100


def test_dynamic_rows_combine_with_exclusions_and_ids():
    s = {"type": "criteria", "studioSceneCount": {"max": 2}, "excludeTags": ["9320"],
         "performers": ["7"]}
    assert criteria.validate(s) == []
    f = criteria.build_scene_filter(s)
    assert f["tags"]["excludes"] == ["9320"]
    assert f["performers"]["value"] == ["7"]
    assert f["studios_filter"]["scene_count"]["modifier"] == "LESS_THAN"


def test_dynamic_rows_validation_and_membership():
    assert criteria.validate({"type": "criteria", "studioSceneCount": {}})[0][
        "code"] == "bad_scene_count"
    assert criteria.validate({"type": "criteria", "performerSceneCount": {"min": "x"}})[0][
        "code"] == "bad_scene_count"
    assert criteria.validate({"type": "criteria", "performerSceneCount": {"min": 5, "max": 2}})[0][
        "code"] == "bad_scene_count"
    # a dynamic row alone is a valid pool (not "empty rules")
    assert criteria.validate({"type": "criteria", "studioSceneCount": {"max": 2}}) == []
    # unknown fields near the dynamic rows are still caught
    result = criteria.validate({"type": "criteria", "studioSceneCount": {"max": 2}, "wut": 1})
    assert any(e["code"] == "unknown_fields" for e in result)


def test_dynamic_rows_summary_and_signature():
    lines = criteria.summarize({"type": "criteria", "studioSceneCount": {"max": 2}})
    assert "fewer than 2 scenes" in lines[-1]
    a = criteria.source_signature({"type": "criteria", "studioSceneCount": {"max": 2}})
    b = criteria.source_signature({"type": "criteria", "studioSceneCount": {"max": 3}})
    assert a != b


# ---------------------------------------------------------------------------
# channel organization: PreviewChannelArrangement + the renumber opcode
# ---------------------------------------------------------------------------


def arrangement_library(tmp_path):
    """Three consecutive networks (300-302) + one custom at 12."""
    doc = {
        "schemaVersion": library_mod.STORAGE_VERSION,
        "libraryId": "lib_arrange0",
        "revision": 12,
        "groups": [
            {"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
            {"id": "grp_net", "name": "Networks", "position": 2, "legacySection": None},
        ],
        "channels": [
            {"id": "ch_11111111", "kind": "ch", "number": 12, "name": "Custom",
             "glyph": None, "color": "#112233", "groupId": "grp_my",
             "sort": "shuffle", "seed": 9, "enabled": True, "archived": False,
             "paused": False, "source": {"type": "tag", "id": "5", "ids": ["5"]},
             "sourceLabel": "tag", "programming": {"mode": "fixed"},
             "provenance": {"origin": "custom"}},
        ],
        "recentRequests": [],
    }
    for i, (cid, number) in enumerate((
            ("net_aaaa0001", 300), ("net_aaaa0002", 301), ("net_aaaa0003", 302))):
        doc["channels"].append({
            "id": cid, "kind": "net", "number": number, "name": f"Net {number}",
            "glyph": "", "color": "#445566", "groupId": "grp_net",
            "sort": "newest", "seed": 1000 + i, "enabled": True,
            "archived": False, "paused": False,
            "source": {"type": "criteria", "studiosAny": [str(10 + i)]},
            "sourceLabel": "", "programming": {"mode": "fixed"},
            "provenance": {"origin": "v4-final-proposal"}})
    library_mod.save(tmp_path, doc)
    return doc


def test_renumber_only_apply_is_fully_cosmetic(tmp_path):
    """An arrangement moves numbers and NOTHING else: no refresh journal, no
    rotation churn, no publication rewrite, membership stable, presentation
    moved."""
    from justwatch import channel_service, contract, lineup
    doc = arrangement_library(tmp_path)
    nets = [c for c in doc["channels"] if c["kind"] == "net"]
    before = {c["id"]: {
        "rotation": lineup.rotation_version(c["source"], c["sort"], c["seed"],
                                            contract.ROTATION_SIZE),
        "membership": criteria.source_signature(c["source"]),
        "presentation": channel_service.presentation_signature(c),
    } for c in nets}
    pub = tmp_path / "programming" / "net_aaaa0001.json"
    pub.parent.mkdir()
    pub.write_text('{"published": true}', encoding="utf-8")

    ctx = Ctx(tmp_path, FakeClient(), {})
    ctx.args = {"requestId": "arrange-1", "expectedRevision": 12, "ops": [
        {"op": "channels.renumber", "assignments": [
            {"channelId": "net_aaaa0001", "number": 303},
            {"channelId": "net_aaaa0002", "number": 304},
            {"channelId": "net_aaaa0003", "number": 305}]}]}
    receipt = channel_ops.op_apply_channel_changes(ctx)
    assert receipt["status"] == "committed"
    assert receipt["refresh"] == {}, "a renumber must never reindex"
    assert refresh.read_pending(tmp_path) == []
    after = {c["id"]: c for c in library_mod.load(tmp_path)["channels"]}
    for cid, was in before.items():
        channel = after[cid]
        assert channel["number"] != next(
            c["number"] for c in nets if c["id"] == cid), "sanity: it moved"
        assert lineup.rotation_version(channel["source"], channel["sort"],
                                       channel["seed"], contract.ROTATION_SIZE) \
            == was["rotation"], "rotation identity unchanged"
        assert criteria.source_signature(channel["source"]) == was["membership"]
        assert channel_service.presentation_signature(channel) \
            != was["presentation"], "presentation moves by design"
        assert channel["seed"] == next(
            c["seed"] for c in nets if c["id"] == cid), "seed untouched"
    assert pub.read_text(encoding="utf-8") == '{"published": true}', \
        "publications are keyed by id and never rewritten by an arrangement"


def test_preview_arrangement_read_only_and_correlated(tmp_path):
    from justwatch import channel_service
    arrangement_library(tmp_path)
    library_file = tmp_path / "channel-library.json"
    before = library_file.read_bytes()
    intent = {"type": "insert", "channelId": "net_aaaa0002", "number": 300}

    stale = channel_ops.op_preview_channel_arrangement(Ctx(
        tmp_path, FakeClient(),
        {"expectedRevision": 11, "correlationToken": "org-tok",
         "intent": intent}))
    assert stale["valid"] is False
    assert stale["errors"][0]["code"] == "stale_revision"
    assert stale["errors"][0]["currentRevision"] == 12
    assert stale["correlationToken"] == "org-tok", "token echoed verbatim"

    body = channel_ops.op_preview_channel_arrangement(Ctx(
        tmp_path, FakeClient(),
        {"expectedRevision": 12, "correlationToken": "org-tok",
         "intent": intent, "explain": True}))
    assert body["valid"] and not body["noop"]
    assert body["revision"] == 12 and body["correlationToken"] == "org-tok"
    got = {(c["channelId"] or c["tempRef"]): (c["from"], c["to"])
           for c in body["numberChanges"]}
    # the 301 row inserts at 300: its own old slot is the first free number
    # above, so only the 300 holder shifts (mover removed from occupancy)
    assert got["net_aaaa0002"] == (301, 300)
    assert got["net_aaaa0001"] == (300, 301)
    assert body["choices"]["swap"]["withName"] == "Net 300"
    assert library_file.read_bytes() == before, "zero writes on preview"
    doc = library_mod.load(tmp_path)
    assert [c["id"] for c in doc["channels"]] == \
        [c["id"] for c in arrangement_library(tmp_path)["channels"]], \
        "no identity allocation, no reorder"
    assert doc["revision"] == 12 and doc["recentRequests"] == []

    with pytest.raises(ValueError):
        channel_ops.op_preview_channel_arrangement(Ctx(
            tmp_path, FakeClient(),
            {"expectedRevision": 12, "correlationToken": "", "intent": intent}))


def test_preview_arrangement_requires_migrated_library(tmp_path):
    with pytest.raises(ValueError):
        channel_ops.op_preview_channel_arrangement(Ctx(
            tmp_path, FakeClient(),
            {"expectedRevision": 1, "correlationToken": "t",
             "intent": {"type": "swap", "a": "ch_11111111",
                        "b": "ch_22222222"}}))


def test_effect_summary_renumber_is_metadata(tmp_path):
    arrangement_library(tmp_path)
    ctx = Ctx(tmp_path, FakeClient(), {"ops": [
        {"op": "channels.renumber", "assignments": [
            {"channelId": "net_aaaa0001", "number": 303},
            {"channelId": "net_aaaa0003", "number": 300}]}]})
    result = channel_ops.op_validate_channel_changes(ctx)
    assert result["valid"] is True, result["errors"]
    effects = [e for e in result["effects"] if e["op"] == "channels.renumber"]
    assert effects == [{"op": "channels.renumber", "kind": "metadata",
                        "reindex": False, "count": 2}]
    assert len(ctx.client.queries) == 0, \
        "organization-only validation never queries Stash"


def test_capabilities_advertise_arrangement():
    from justwatch import contract
    features = contract.capabilities("0.0.0-test")["features"]
    assert features["arrangement"] == {
        "version": 1,
        "previewOperation": "PreviewChannelArrangement",
        "renumberOpcode": "channels.renumber",
    }
    assert contract.OPERATIONS["previewChannelArrangement"] == \
        "PreviewChannelArrangement"
    assert "previewChannelArrangement" in contract.SYNC_OPERATIONS
