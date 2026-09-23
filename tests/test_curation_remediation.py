"""Post-fix behavioral regressions for the 2026-09-23 channel-curation audit
(C1–C12). Each test asserts the INTENDED outcome for a defect the audit
reproduced independently; the audit's own `analysis/channel-curation-audit/`
scripts remain as pre-fix evidence.

Covers: transaction atomicity and identity (C3/C4), durable refresh work and
health merging (C5), source/playback parity and preview truth (C2/C11),
schedule/build guards (C6), programming-mode parity (C7).
"""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from justwatch import (channel_ops, continuing, criteria, library, lineup,  # noqa: E402
                       main, programming, refresh, snapshots)


def fixture_library() -> dict:
    return {
        "schemaVersion": 1, "libraryId": "lib_audit", "revision": 1,
        "groups": [{"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
                   {"id": "grp_general", "name": "General", "position": 2, "legacySection": "general"}],
        "channels": [dict(id=f"ch_{i:08x}", kind="ch", number=i, name=f"Channel {i}",
                          glyph=None, color="#112233", groupId="grp_my", sort="shuffle", seed=i,
                          enabled=True, archived=False, paused=False,
                          source={"type": "tag", "id": "5", "ids": ["5"]},
                          sourceLabel="Tag 5", programming={"mode": "fixed"},
                          provenance={"origin": "custom"})
                     for i in (1, 2)],
        "settings": {}, "recentRequests": [],
    }


class FakeClient:
    """Synthetic Stash: one playable scene per query plus entity lookups."""

    def __init__(self, playable=True, entities_exist=True):
        self.calls = []
        self.playable = playable
        self.entities_exist = entities_exist

    def submit(self, query, variables):
        self.calls.append(copy.deepcopy(variables))
        if "findScenes" in query:
            return {"findScenes": {"count": 100, "scenes": [
                {"id": "scene-old", "title": "Synthetic", "studio": {"name": "S"},
                 "files": [{"duration": 600}] if self.playable else [],
                 "paths": {"preview": "/mock.jpg"}}]}}
        if "NestedFilterSupport" in query:
            if not self.entities_exist:
                raise ValueError("unknown field")
            return {"findScenes": {"count": 0}}
        if "findTag" in query:
            node = {"name": "Tag"} if self.entities_exist else {}
            return {"findTag": node}
        if "findPerformer" in query:
            node = {"name": "Performer"} if self.entities_exist else {}
            return {"findPerformer": node}
        if "findStudio" in query:
            node = {"name": "Studio"} if self.entities_exist else {}
            return {"findStudio": node}
        return {}


def ctx(p, args=None, client=None):
    return SimpleNamespace(data_dir=p, assets_dir=p / "assets", args=args or {},
                           client=client or FakeClient())


def saved(tmp_path):
    library.save(tmp_path, fixture_library())
    return tmp_path


# ---------------------------------------------------------------------------
# C2 — one projection everywhere
# ---------------------------------------------------------------------------

class TestSourcePlaybackParity:
    def test_criteria_source_reaches_lineup_and_both_indexers(self, tmp_path):
        d = fixture_library()
        d["channels"][0]["source"] = {"type": "criteria", "tagsAny": ["5"]}
        library.save(tmp_path, d)
        c = ctx(tmp_path, {"channelId": d["channels"][0]["id"],
                           "source": d["channels"][0]["source"]})
        lineup_page = main._op_lineup(c)          # must not raise unknown source
        assert lineup_page["sourceTotal"] == 100
        entries = programming.index_source(c.client, d["channels"][0])
        assert entries and entries[0]["id"] == "scene-old"
        entries2 = continuing.index_source(c.client, d["channels"][0])
        assert entries2 and entries2[0]["id"] == "scene-old"

    def test_edited_filter_keeps_every_authored_rule_at_playback(self):
        source = {"type": "filter", "tags": ["5"], "excludeStudios": ["9"],
                  "duration": {"max": 900}, "studioSceneCount": {"max": 2}}
        assert criteria.validate(source) == []
        editor = criteria.build_scene_filter(source)
        playback = lineup.build_scene_filter(source)
        assert playback == editor, "editor preview and playback must agree"
        assert playback["studios"]["excludes"] == ["9"]
        assert playback["duration"]["value2"] == 900
        assert "studios_filter" in playback

    def test_distinct_criteria_sources_have_distinct_identity(self):
        a = {"type": "criteria", "tagsAny": ["5"]}
        b = {"type": "criteria", "tagsAny": ["6"]}
        assert lineup.source_key(a) != lineup.source_key(b)
        assert lineup.rotation_version(a, "shuffle", 1, 50) != \
            lineup.rotation_version(b, "shuffle", 1, 50)

    def test_authored_q_rides_every_query_path(self, tmp_path):
        source = {"type": "criteria", "tagsAny": ["5"], "q": "must-match"}
        client = FakeClient()
        lineup.fetch_rotation(client, source=source, sort="shuffle", seed=1, size=10)
        assert all(v["filter"].get("q") == "must-match" for v in client.calls)
        client2 = FakeClient()
        channel = {"source": source, "sort": "shuffle", "seed": 1}
        programming.index_source(client2, channel)
        assert all(v["filter"].get("q") == "must-match" for v in client2.calls)
        client3 = FakeClient()
        continuing.index_source(client3, channel)
        assert all(v["filter"].get("q") == "must-match" for v in client3.calls)

    def test_dynamic_max_is_exclusive_including_combined_bounds(self):
        only_max = criteria.build_scene_filter({"type": "criteria", "studioSceneCount": {"max": 2}})
        assert only_max["studios_filter"]["scene_count"] == \
            {"value": 2, "value2": None, "modifier": "LESS_THAN"}
        both = criteria.build_scene_filter({"type": "criteria", "studioSceneCount": {"min": 1, "max": 2}})
        assert both["studios_filter"]["scene_count"] == \
            {"value": 1, "value2": 1, "modifier": "BETWEEN"}, \
            "max is exclusive: [1, 2) converts to BETWEEN 1..1"


# ---------------------------------------------------------------------------
# C3 — atomic transactions + exact replay of rejected receipts
# ---------------------------------------------------------------------------

class TestTransactionAtomicity:
    def test_rejected_transaction_leaves_definitions_untouched_and_replays(self, tmp_path):
        saved(tmp_path)
        before = library.load(tmp_path)
        ops = [
            {"op": "channels.patch", "channelIds": ["ch_00000001"], "patch": {"paused": True}},
            {"op": "channel.create", "tempId": "a", "channel": {
                "kind": "net", "number": 500, "name": "A", "groupId": "grp_general",
                "source": {"type": "tag", "id": "5"}}},
            {"op": "channel.create", "tempId": "b", "channel": {
                "kind": "net", "number": 500, "name": "B", "groupId": "grp_general",
                "source": {"type": "tag", "id": "5"}}},
        ]
        receipt = library.apply_transaction(
            tmp_path, expected_revision=1, request_id="partial", ops=ops)
        assert receipt["status"] == "rejected"
        after = library.load(tmp_path)
        assert after["revision"] == 1
        assert after["channels"] == before["channels"], \
            "a rejected transaction must not persist any staged operation"
        assert after["channels"][0]["paused"] is False
        assert "digest" in receipt, "rejected receipts replay exactly"
        retry = library.apply_transaction(
            tmp_path, expected_revision=1, request_id="partial", ops=ops)
        assert retry["status"] == "rejected"
        assert retry.get("error") == receipt["error"] != "request_replayed_with_different_content"

    def test_mid_transaction_race_rejects_without_partial_state(self, tmp_path):
        saved(tmp_path)
        ops = [
            {"op": "channels.move", "channelIds": ["ch_00000001"], "groupId": "grp_general"},
            {"op": "group.delete", "id": "grp_my"},  # ch_00000002 still holds it -> race
        ]
        receipt = library.apply_transaction(
            tmp_path, expected_revision=1, request_id="race", ops=ops)
        assert receipt["status"] == "rejected"
        doc = library.load(tmp_path)
        assert doc["revision"] == 1
        assert doc["channels"][0]["groupId"] == "grp_my"
        assert {g["id"] for g in doc["groups"]} == {"grp_my", "grp_general"}


# ---------------------------------------------------------------------------
# C4 — identity + allowed fields through every opcode
# ---------------------------------------------------------------------------

class TestIdentityEnforcement:
    @pytest.mark.parametrize("patch", [
        {"seed": 9876}, {"id": "ch_99999999"}, {"kind": "net"},
        {"provenance": {"origin": "evil"}},
        {"name": "ok", "seed": 1},  # allowed key + forbidden key
        {"paused": "yes"},  # non-boolean
        {"hack": True},  # unknown key
    ])
    def test_bulk_patch_cannot_smuggle_identity_or_bad_fields(self, tmp_path, patch):
        saved(tmp_path)
        before = library.load(tmp_path)
        receipt = library.apply_transaction(tmp_path, expected_revision=1,
                                            request_id="p", ops=[
            {"op": "channels.patch", "channelIds": ["ch_00000001"], "patch": patch}])
        assert receipt["status"] == "rejected", patch
        after = library.load(tmp_path)
        assert after["channels"] == before["channels"]

    def test_channel_put_cannot_change_seed_or_kind(self, tmp_path):
        saved(tmp_path)
        draft = copy.deepcopy(fixture_library()["channels"][0])
        draft["seed"] = 4242
        receipt = library.apply_transaction(tmp_path, expected_revision=1,
                                            request_id="put", ops=[
            {"op": "channel.put", "channel": draft}])
        assert receipt["status"] == "rejected"
        assert any(e["code"] == "identity_change" for e in receipt["errors"])

    def test_final_candidate_identity_enforced_even_past_op_validation(self, tmp_path):
        # Belt-and-braces: _enforce_identity compares the committed candidate
        # against the original document whatever opcode built it.
        saved(tmp_path)
        original = library.load(tmp_path)
        candidate = copy.deepcopy(original)
        candidate["channels"][0]["seed"] = 31337
        with pytest.raises(Exception):
            library._enforce_identity(original, candidate)


# ---------------------------------------------------------------------------
# C5 — durable work + health merging
# ---------------------------------------------------------------------------

class TestDurableRefreshWork:
    def test_intent_journal_precedes_commit_no_crash_window(self, tmp_path):
        saved(tmp_path)
        draft = copy.deepcopy(fixture_library()["channels"][0])
        draft["source"] = {"type": "tag", "id": "6", "ids": ["6"]}
        before = {c["id"]: c for c in library.load(tmp_path)["channels"]}
        # Simulated process loss DURING the pre-commit journal write: the
        # whole transaction aborts — nothing commits, nothing is pending.
        with patch.object(refresh, "enqueue",
                          side_effect=RuntimeError("simulated loss")):
            with pytest.raises(RuntimeError):
                library.apply_transaction(
                    tmp_path, expected_revision=1, request_id="crash",
                    ops=[{"op": "channel.put", "channel": draft}],
                    pre_commit=refresh.commit_hook(tmp_path, before))
        doc = library.load(tmp_path)
        assert doc["revision"] == 1
        assert doc["channels"][0]["source"] == {"type": "tag", "id": "5", "ids": ["5"]}
        # the retry commits AND journals atomically-ordered
        receipt = library.apply_transaction(
            tmp_path, expected_revision=1, request_id="crash",
            ops=[{"op": "channel.put", "channel": draft}],
            pre_commit=refresh.commit_hook(
                tmp_path, {c["id"]: c for c in doc["channels"]}))
        assert receipt["status"] == "committed"
        pending = refresh.read_pending(tmp_path)
        assert [e["channelId"] for e in pending] == ["ch_00000001"]

    def test_apply_task_journals_and_drains_end_to_end(self, tmp_path):
        saved(tmp_path)
        draft = copy.deepcopy(fixture_library()["channels"][0])
        draft["source"] = {"type": "tag", "id": "6", "ids": ["6"]}
        result = channel_ops.op_apply_channel_changes(ctx(tmp_path, {
            "requestId": "apply-1", "expectedRevision": 1,
            "ops": json.dumps([{"op": "channel.put", "channel": draft}])}))
        assert result["status"] == "committed"
        assert result["refresh"].get("ch_00000001") in ("refreshed", "current")
        entry = snapshots.read_snapshot(tmp_path, tmp_path / "assets")["channels"]["ch_00000001"]
        assert entry["sourceSignature"] == criteria.source_signature(draft["source"])
        assert refresh.read_pending(tmp_path) == []

    def test_concurrent_enqueue_survives_a_worker_run(self, tmp_path):
        saved(tmp_path)
        signatures = {c["id"]: criteria.source_signature(c["source"])
                      for c in library.load(tmp_path)["channels"]}
        refresh.enqueue(tmp_path, ["ch_00000001"], 1, signatures)

        def health(*args, **kwargs):
            # a concurrent Apply enqueues channel 2 while the worker runs
            refresh.enqueue(tmp_path, ["ch_00000002"], 1, signatures)
            return {"status": "ok", "count": 1, "sourceTotal": 1}

        with patch.object(snapshots, "_channel_health", side_effect=health):
            result = refresh.process_pending(FakeClient(), tmp_path, tmp_path / "assets")
        remaining = {e["channelId"] for e in refresh.read_pending(tmp_path)}
        assert remaining == {"ch_00000002"}, \
            "concurrently enqueued work must never be deleted by the worker"

    def test_two_channel_batch_keeps_both_health_records(self, tmp_path):
        saved(tmp_path)
        signatures = {c["id"]: criteria.source_signature(c["source"])
                      for c in library.load(tmp_path)["channels"]}
        refresh.enqueue(tmp_path, list(signatures), 1, signatures)
        with patch.object(snapshots, "_channel_health",
                          return_value={"status": "ok", "count": 1, "sourceTotal": 1,
                                        "sourceSignature": signatures["ch_00000001"]}):
            refresh.process_pending(FakeClient(), tmp_path, tmp_path / "assets")
        merged = snapshots.read_snapshot(tmp_path, tmp_path / "assets").get("channels", {})
        assert set(merged) == {"ch_00000001", "ch_00000002"}, \
            "health publication merges into the CURRENT snapshot per channel"

    def test_metadata_only_apply_never_reindexes(self, tmp_path):
        saved(tmp_path)
        draft = copy.deepcopy(fixture_library()["channels"][0])
        draft["name"] = "Renamed only"
        client = FakeClient()
        result = channel_ops.op_apply_channel_changes(ctx(tmp_path, {
            "requestId": "meta", "expectedRevision": 1,
            "ops": json.dumps([{"op": "channel.put", "channel": draft}])}, client))
        assert result["status"] == "committed"
        assert "no reindex" in result["note"]
        assert refresh.read_pending(tmp_path) == []
        assert not any("findScenes" in json.dumps(c) for c in client.calls), \
            "a cosmetic Apply must not query scenes"


# ---------------------------------------------------------------------------
# C6 — stale schedules and stale workers
# ---------------------------------------------------------------------------

class TestScheduleAndBuildGuards:
    def _publish(self, tmp_path, channel, source):
        pub = {"schema": 1, "source": source, "configuration": programming.digest(
            [source, channel.get("sort", "shuffle"), channel["seed"],
             programming.policy(channel.get("programming"))]),
            "version": "old", "sourceTotal": 1,
            "preparedThrough": 2_000_000, "warnings": [], "generatedAt": 1,
            "generation": "g1", "programs": [
                {"airingId": "air-old", "startEpochMs": 500_000, "endEpochMs": 1_500_000,
                 "item": {"id": "scene-from-removed-pool", "duration": 600}, "block": ""}],
            "index": [], "indexedAt": 1}
        snapshots.write_json(programming.path(tmp_path, channel["id"]), pub)
        return pub

    def test_source_incompatible_publication_is_not_served(self, tmp_path):
        d = fixture_library()
        d["channels"][0]["programming"] = {"mode": "explore"}
        library.save(tmp_path, d)
        self._publish(tmp_path, d["channels"][0], {"type": "tag", "id": "5", "ids": ["5"]})
        changed = copy.deepcopy(d["channels"][0])
        changed["source"] = {"type": "tag", "id": "999", "ids": ["999"]}
        library.apply_transaction(tmp_path, expected_revision=1, request_id="pool",
                                  ops=[{"op": "channel.put", "channel": changed}])
        at = 600_000  # inside the old airing
        result = main._op_schedule(ctx(tmp_path, {"channelId": "ch_00000001", "at": at}))
        assert result["status"] == "preparing"
        assert result.get("stalePublication") is True
        # the running airing finishes; no FUTURE scene from the removed pool airs
        assert [p["item"]["id"] for p in result["programs"]] == ["scene-from-removed-pool"]
        future = main._op_schedule(ctx(tmp_path, {"channelId": "ch_00000001", "at": 1_600_000}))
        assert future["status"] == "preparing"
        assert future["programs"] == []

    def test_stale_worker_cannot_publish_over_a_policy_edit(self, tmp_path):
        d = fixture_library()
        d["channels"][0]["programming"] = {"mode": "explore"}
        library.save(tmp_path, d)
        indexed = copy.deepcopy(d["channels"][0])

        def build(*args, **kwargs):
            edited = copy.deepcopy(indexed)
            edited["sort"] = "newest"
            library.apply_transaction(tmp_path, expected_revision=1, request_id="new-sort",
                                      ops=[{"op": "channel.put", "channel": edited}])
            return {"schema": 1, "programs": [], "configuration": "stale"}

        with patch.object(programming, "index_source", return_value=[]), \
             patch.object(programming, "build", side_effect=build):
            outcome = refresh._prepare_one(FakeClient(), tmp_path, indexed)
        assert outcome == "changed_during_build"
        assert not programming.path(tmp_path, indexed["id"]).exists() or \
            json.loads(programming.path(tmp_path, indexed["id"]).read_text()).get("configuration") != "stale"


# ---------------------------------------------------------------------------
# C7 — one effective mode, lossless policy
# ---------------------------------------------------------------------------

class TestProgrammingModeParity:
    def test_fixed_pin_beats_rollout_everywhere_and_policy_survives_rename(self, tmp_path):
        d = fixture_library()
        n = d["channels"][0]
        n.update(id="net_00000001", kind="net", number=500, groupId="grp_general",
                 programming={"mode": "fixed", "newShare": 0.2})
        library.save(tmp_path, d)
        snapshots.write_json(tmp_path / "continuing-networks.json",
                             {"enabled": True, "stage": "active", "networkIds": [n["id"]]})
        enhanced = channel_ops.op_get_channel_directory(ctx(tmp_path))
        legacy = main._op_directory(ctx(tmp_path))
        schedule = main._op_schedule(ctx(tmp_path, {"channelId": n["id"]}))
        modes = {
            "enhanced": next(c["programmingMode"] for c in enhanced["channels"] if c["id"] == n["id"]),
            "legacy": legacy["networks"]["channels"][0]["programmingMode"],
            "schedule": schedule["status"],
        }
        assert modes == {"enhanced": "fixed", "legacy": "fixed", "schedule": "fixed"}
        # cosmetic rename: policy preserved losslessly
        draft = copy.deepcopy(n)
        draft["name"] = "Renamed only"
        library.apply_transaction(tmp_path, expected_revision=1, request_id="rename",
                                  ops=[{"op": "channel.put", "channel": draft}])
        stored = next(c for c in library.load(tmp_path)["channels"] if c["id"] == n["id"])
        assert stored["programming"] == {"mode": "fixed", "newShare": 0.2}

    def test_unservable_mode_change_is_rejected_per_namespace(self, tmp_path):
        saved(tmp_path)
        draft = copy.deepcopy(fixture_library()["channels"][0])
        draft["programming"] = {"mode": "continuing"}  # ch_ namespace: no engine serves it
        receipt = library.apply_transaction(tmp_path, expected_revision=1,
                                            request_id="mode", ops=[
            {"op": "channel.put", "channel": draft}])
        assert receipt["status"] == "rejected"
        assert any(e.get("code") == "bad_mode_for_kind" for e in receipt.get("errors", []))

    def test_group_create_plus_move_is_one_atomic_transaction(self, tmp_path):
        saved(tmp_path)
        receipt = library.apply_transaction(tmp_path, expected_revision=1,
                                            request_id="group-move", ops=[
            {"op": "group.put", "group": {"id": "grp_new01", "name": "Fresh", "position": 3}},
            {"op": "channels.move", "channelIds": ["ch_00000001"], "groupId": "grp_new01"}])
        assert receipt["status"] == "committed"
        doc = library.load(tmp_path)
        assert doc["channels"][0]["groupId"] == "grp_new01"


# ---------------------------------------------------------------------------
# C11 — preview truth + strict validation
# ---------------------------------------------------------------------------

class TestPreviewTruth:
    def test_preview_reports_actual_playable_rotation_and_carries_q(self, tmp_path):
        client = FakeClient(playable=False)  # every row unplayable
        result = channel_ops.op_preview_channel_pool(ctx(tmp_path, {
            "source": {"type": "criteria", "tagsAny": ["5"], "q": "must-match"}}, client))
        assert result["poolCount"] == 100
        assert result["rotationSize"] == 0, "an all-unplayable pool has no rotation"
        assert result["rotationComplete"] is True  # the scan exhausted the source
        assert all(v["filter"].get("q") == "must-match" for v in client.calls)

    def test_missing_entity_reference_is_rejected_at_validation(self, tmp_path):
        saved(tmp_path)
        draft = copy.deepcopy(fixture_library()["channels"][0])
        draft["source"] = {"type": "studio", "id": "999999999999"}
        client = FakeClient(entities_exist=False)
        result = channel_ops.op_validate_channel_changes(ctx(tmp_path, {
            "ops": json.dumps([{"op": "channel.put", "channel": draft}])}, client))
        assert result["valid"] is False
        assert any(e["code"] == "missing_entity" for e in result["errors"])

    def test_metadata_only_edit_of_broken_source_stays_recoverable(self, tmp_path):
        d = fixture_library()
        d["channels"][0]["source"] = {"type": "studio", "id": "999999999999"}
        library.save(tmp_path, d)
        draft = copy.deepcopy(d["channels"][0])
        draft["name"] = "Renamed"
        client = FakeClient(entities_exist=False)
        result = channel_ops.op_validate_channel_changes(ctx(tmp_path, {
            "ops": json.dumps([{"op": "channel.put", "channel": draft}])}, client))
        assert result["valid"] is True

    def test_mixed_and_malformed_id_lists_are_typed_errors(self):
        errors = criteria.validate({"type": "criteria", "tagsAny": ["5", "bad-id"]})
        assert any(e["code"] == "bad_source_ids" for e in errors)

    def test_impossible_calendar_date_is_rejected(self):
        errors = criteria.validate({"type": "criteria", "date": {"from": "2026-99-99"}})
        assert any(e["code"] == "bad_date" for e in errors)

    def test_dynamic_rules_on_old_stash_fail_typed_at_validation_and_preview(self, tmp_path):
        saved(tmp_path)
        draft = copy.deepcopy(fixture_library()["channels"][0])
        draft["source"] = {"type": "criteria", "tagsAny": ["5"],
                           "studioSceneCount": {"max": 2}}
        client = FakeClient(entities_exist=False)  # nested probe fails
        result = channel_ops.op_validate_channel_changes(ctx(tmp_path, {
            "ops": json.dumps([{"op": "channel.put", "channel": draft}])}, client))
        assert any(e["code"] == "unsupported_dynamic_rules" for e in result["errors"])
        with pytest.raises(ValueError, match="not supported by this Stash"):
            channel_ops.op_preview_channel_pool(ctx(tmp_path, {
                "source": draft["source"]}, client))
