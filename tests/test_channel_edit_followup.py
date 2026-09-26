"""Post-audit behavioral regressions for the 2026-09-24 channel-edit audit —
the backend half the Channel Studio UX updates depend on:

* E8 — explicit-zero duration bounds survive normalize/projection/validation
  end-to-end (blank ≠ zero), with typed errors for negative/overflowing input.
* E9 — the recency clock is injectable so tests pin a day instead of racing
  the calendar (fixed in test_channel_ops.py; the rollover lives here).
* E7 — a successful empty query for a rule source is OFF AIR, not
  "missing source"; a FAILED existence lookup for a linked source is
  "unavailable" (stale, retryable), not "missing".
* E2 (minimal) — an unavailable health result is a RETRYABLE stage failure:
  the journal entry survives, last-known counts stay published, and a
  forced one-channel requeue recomputes even health-current channels.
* GetChannelRefreshStatus / RequeueChannelRefresh — the durable post-Apply
  truth the editor's refresh pill reads, and the Retry behind it.
"""
import copy
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from justwatch import channel_ops, criteria, library, lineup, refresh, snapshots  # noqa: E402


def fixture_library() -> dict:
    return {
        "schemaVersion": 1, "libraryId": "lib_followup", "revision": 1,
        "groups": [{"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None}],
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
    """Synthetic Stash: scripted scene count + entity lookups."""

    def __init__(self, scene_count=100, entities_exist=True):
        self.calls = []
        self.scene_count = scene_count
        self.entities_exist = entities_exist

    def submit(self, query, variables):
        self.calls.append(copy.deepcopy(variables))
        if "findScenes" in query:
            scenes = ([{"id": "scene-old", "title": "Synthetic", "studio": {"name": "S"},
                        "files": [{"duration": 600}], "paths": {"preview": "/mock.jpg"}}]
                      * min(1, self.scene_count)) if self.scene_count else []
            return {"findScenes": {"count": self.scene_count, "scenes": scenes}}
        if "findTag" in query:
            return {"findTag": {"name": "Tag"} if self.entities_exist else {}}
        if "findPerformer" in query:
            return {"findPerformer": {"name": "Performer"} if self.entities_exist else {}}
        if "findStudio" in query:
            return {"findStudio": {"name": "Studio"} if self.entities_exist else {}}
        return {}


class RaisingLookupClient(FakeClient):
    """A linked-source existence lookup that fails at transport level."""

    def submit(self, query, variables):
        if "findTag" in query:
            raise RuntimeError("stash down")
        return super().submit(query, variables)


def ctx(p, args=None, client=None):
    return SimpleNamespace(data_dir=p, assets_dir=p / "assets", args=args or {},
                           client=client or FakeClient())


def saved(tmp_path):
    library.save(tmp_path, fixture_library())
    return tmp_path


def signatures_of(doc=None) -> dict:
    doc = doc or fixture_library()
    return {c["id"]: criteria.source_signature(c["source"]) for c in doc["channels"]}


# ---------------------------------------------------------------------------
# E8 — explicit-zero duration bounds
# ---------------------------------------------------------------------------

class TestExplicitZeroDuration:
    def test_zero_max_is_a_real_bound_not_absence(self):
        s = {"type": "criteria", "duration": {"max": 0}}
        assert criteria.validate(s) == []
        assert criteria.normalize(s)["duration"] == {"max": 0}
        # projects to an intentionally EMPTY pool, never a widened INT_MAX
        assert criteria.build_scene_filter(s)["duration"] == {
            "value": 0, "value2": 0, "modifier": "BETWEEN"}

    def test_zero_min_survives_with_its_sibling_bound(self):
        s = {"type": "criteria", "duration": {"min": 0, "max": 3600}}
        assert criteria.validate(s) == []
        assert criteria.normalize(s)["duration"] == {"min": 0, "max": 3600}
        assert criteria.build_scene_filter(s)["duration"] == {
            "value": 0, "value2": 3600, "modifier": "BETWEEN"}

    def test_zero_only_criteria_is_valid_and_stable(self):
        s = {"type": "criteria", "duration": {"max": 0}}
        # a zero-only rule set is a valid (empty) pool, not empty_rules
        assert criteria.validate(s) == []
        once = criteria.normalize(s)
        assert criteria.normalize(once) == once, "normalize is idempotent"
        # identity is stable through persistence and distinct from "absent"
        assert criteria.source_signature(s) == criteria.source_signature(once)
        assert criteria.source_signature(s) != criteria.source_signature({"type": "criteria"})

    def test_blank_and_zero_are_distinct(self):
        assert criteria.normalize({"type": "criteria", "duration": {}}) == {"type": "criteria"}
        assert "duration" not in criteria.normalize({"type": "criteria", "duration": {}})

    def test_sub_minute_bounds_keep_their_precision(self):
        s = {"type": "criteria", "duration": {"min": 90}}
        assert criteria.build_scene_filter(s)["duration"]["value"] == 90
        assert "1.5 min" in criteria.summarize(s)[0]

    def test_negative_and_overflowing_bounds_are_typed_errors(self):
        neg = criteria.validate({"type": "criteria", "duration": {"min": -60}})
        assert any(e["code"] == "bad_duration" and e["path"].endswith(".min") for e in neg)
        over = criteria.validate({"type": "criteria", "duration": {"max": 2_147_483_648}})
        assert any(e["code"] == "bad_duration" and "largest" in e["message"] for e in over)

    def test_min_above_max_is_still_rejected(self):
        errors = criteria.validate({"type": "criteria", "duration": {"min": 3600, "max": 0}})
        assert any(e["code"] == "bad_duration" and "below" in e["message"] for e in errors)


# ---------------------------------------------------------------------------
# E9 — injectable recency clock (rollover)
# ---------------------------------------------------------------------------

class TestRecencyClock:
    def test_cutoff_rolls_with_the_injected_day(self):
        day = dt.date(2026, 9, 23)
        base = {"type": "criteria", "tags": ["1"], "createdAt": {"withinDays": 180}}
        assert criteria.build_scene_filter(base, today=day)["created_at"]["value"] == "2026-03-27"
        assert criteria.build_scene_filter(
            base, today=day + dt.timedelta(days=1))["created_at"]["value"] == "2026-03-28"


# ---------------------------------------------------------------------------
# E7 — empty rule pools and failed lookups have distinct health outcomes
# ---------------------------------------------------------------------------

class TestHealthOutcomes:
    def _health(self, source, client=None):
        channel = {"id": "ch_1", "number": 1, "name": "C", "source": source,
                   "sort": "shuffle", "seed": 1}
        return snapshots._channel_health(client or FakeClient(), channel, {})

    def test_empty_rule_pool_is_off_air_not_missing(self):
        # duration 4800s past every scene in the (600s) fixture library:
        # a successful zero-result query for valid rules.
        health = self._health({"type": "criteria", "duration": {"min": 4800}},
                              FakeClient(scene_count=0))
        assert health["healthStatus"] == "offAir"
        assert health["sourceMissing"] is False

    def test_empty_filter_pool_is_off_air_too(self):
        health = self._health({"type": "filter", "tags": ["5"], "excludeTags": ["6"]},
                              FakeClient(scene_count=0))
        assert health["healthStatus"] == "offAir"

    def test_deleted_linked_entity_is_still_missing_source(self):
        health = self._health({"type": "tag", "id": "5", "ids": ["5"]},
                              FakeClient(scene_count=0, entities_exist=False))
        assert health["healthStatus"] == "missingSource"
        assert health["sourceMissing"] is True

    def test_failed_existence_lookup_is_unavailable_not_missing(self):
        health = self._health({"type": "tag", "id": "5", "ids": ["5"]},
                              RaisingLookupClient(scene_count=0))
        assert health["healthStatus"] == "unavailable"


# ---------------------------------------------------------------------------
# E2 (minimal) — failed refresh is retried, not acked-and-forgotten
# ---------------------------------------------------------------------------

class TestRetryableRefresh:
    def test_unavailable_health_stays_queued_and_preserves_last_known(self, tmp_path):
        saved(tmp_path)
        sigs = signatures_of(library.load(tmp_path))
        # last-known numbers from BEFORE the membership change (old signature)
        snapshots.write_snapshot(tmp_path, tmp_path / "assets", {
            "revision": 1, "channels": {"ch_00000001": {
                "healthStatus": "ok", "sceneCount": 7, "loopSeconds": 100.0,
                "loopCapped": False, "sourceMissing": False, "sourceTotal": 7,
                "sourceSignature": "old-sig", "rotationVersion": "rv1"}}})
        refresh.enqueue(tmp_path, ["ch_00000001"], 1, sigs)
        with patch.object(snapshots, "_channel_health", return_value={
                "healthStatus": "unavailable", "sceneCount": 7, "loopSeconds": 100.0,
                "sourceSignature": sigs["ch_00000001"]}):
            result = refresh.process_pending(FakeClient(), tmp_path, tmp_path / "assets")
        assert result["processed"]["ch_00000001"] == "unavailable_retryable"
        assert [e["channelId"] for e in refresh.read_pending(tmp_path)] == ["ch_00000001"], \
            "a failed refresh must stay queued for the next pass"
        merged = snapshots.read_snapshot(tmp_path, tmp_path / "assets")["channels"]["ch_00000001"]
        assert merged["healthStatus"] == "unavailable"
        assert merged["sceneCount"] == 7, "last-known numbers stay visible stale-flagged"

    def test_successful_recovery_drains_the_retry(self, tmp_path):
        saved(tmp_path)
        sigs = signatures_of(library.load(tmp_path))
        refresh.enqueue(tmp_path, ["ch_00000001"], 1, sigs)
        ok_health = {"healthStatus": "ok", "sceneCount": 3, "loopSeconds": 60.0,
                     "loopCapped": False, "sourceMissing": False, "sourceTotal": 3,
                     "sourceSignature": sigs["ch_00000001"], "rotationVersion": "rv2"}
        with patch.object(snapshots, "_channel_health", return_value=ok_health):
            result = refresh.process_pending(FakeClient(), tmp_path, tmp_path / "assets")
        assert result["processed"]["ch_00000001"] == "refreshed"
        assert refresh.read_pending(tmp_path) == []

    def test_forced_requeue_recomputes_even_health_current_channel(self, tmp_path):
        saved(tmp_path)
        doc = library.load(tmp_path)
        sigs = signatures_of(doc)
        channel = doc["channels"][0]
        # health already current for exactly this signature
        snapshots.write_snapshot(tmp_path, tmp_path / "assets", {
            "revision": 1, "channels": {channel["id"]: {
                "healthStatus": "ok", "sceneCount": 1, "loopSeconds": 60.0,
                "sourceSignature": sigs[channel["id"]], "rotationVersion": "rv"}}})
        entry = refresh.requeue(tmp_path, channel, doc["revision"])
        assert entry["force"] is True
        called = []

        def spy(client, ch, previous):
            called.append(ch["id"])
            return {"healthStatus": "ok", "sceneCount": 9, "loopSeconds": 90.0,
                    "sourceSignature": criteria.source_signature(ch["source"]),
                    "rotationVersion": "rv9"}

        with patch.object(snapshots, "_channel_health", side_effect=spy):
            result = refresh.process_pending(FakeClient(), tmp_path, tmp_path / "assets")
        assert called == [channel["id"]], "force defeats the current-health short-circuit"
        assert result["processed"][channel["id"]] == "refreshed"
        assert refresh.read_pending(tmp_path) == [], "a successful forced pass acks itself"
        merged = snapshots.read_snapshot(tmp_path, tmp_path / "assets")["channels"][channel["id"]]
        assert merged["sceneCount"] == 9

    def test_health_current_without_force_does_no_work(self, tmp_path):
        saved(tmp_path)
        doc = library.load(tmp_path)
        sigs = signatures_of(doc)
        channel = doc["channels"][0]
        snapshots.write_snapshot(tmp_path, tmp_path / "assets", {
            "revision": 1, "channels": {channel["id"]: {
                "healthStatus": "ok", "sceneCount": 1, "loopSeconds": 60.0,
                "sourceSignature": sigs[channel["id"]], "rotationVersion": "rv"}}})
        refresh.enqueue(tmp_path, [channel["id"]], 1, sigs)
        with patch.object(snapshots, "_channel_health") as spy:
            result = refresh.process_pending(FakeClient(), tmp_path, tmp_path / "assets")
        spy.assert_not_called()
        assert result["processed"][channel["id"]] == "current"
        assert refresh.read_pending(tmp_path) == []


# ---------------------------------------------------------------------------
# Refresh status + requeue ops
# ---------------------------------------------------------------------------

class TestRefreshStatusOps:
    def test_status_composes_journal_and_health(self, tmp_path):
        saved(tmp_path)
        out = channel_ops.op_get_channel_refresh_status(
            ctx(tmp_path, {"channelId": "ch_00000001"}))
        assert out["pending"] is None and out["health"] is None
        assert out["libraryRevision"] == 1
        sigs = signatures_of(library.load(tmp_path))
        refresh.enqueue(tmp_path, ["ch_00000001"], 1, sigs)
        snapshots.write_snapshot(tmp_path, tmp_path / "assets", {
            "revision": 1, "channels": {"ch_00000001": {"healthStatus": "ok"}}})
        out = channel_ops.op_get_channel_refresh_status(
            ctx(tmp_path, {"channelId": "ch_00000001"}))
        assert out["pending"]["channelId"] == "ch_00000001"
        assert out["pending"]["generation"] == 1
        assert out["health"]["healthStatus"] == "ok"

    def test_status_without_channel_id_summarizes(self, tmp_path):
        saved(tmp_path)
        sigs = signatures_of(library.load(tmp_path))
        refresh.enqueue(tmp_path, ["ch_00000001"], 1, sigs)
        snapshots.write_snapshot(tmp_path, tmp_path / "assets", {
            "revision": 1, "channels": {"ch_00000002": {"healthStatus": "unavailable"}}})
        out = channel_ops.op_get_channel_refresh_status(ctx(tmp_path))
        assert [e["channelId"] for e in out["pending"]] == ["ch_00000001"]
        assert out["healthStatusByChannel"] == {"ch_00000002": "unavailable"}

    def test_requeue_op_enqueues_durable_forced_work(self, tmp_path):
        saved(tmp_path)
        out = channel_ops.op_requeue_channel_refresh(
            ctx(tmp_path, {"channelId": "ch_00000001"}))
        assert out["queued"] is True and out["generation"] == 1
        pending = refresh.read_pending(tmp_path)
        assert len(pending) == 1 and pending[0]["force"] is True
        # a second retry bumps the generation (newest wins)
        out = channel_ops.op_requeue_channel_refresh(
            ctx(tmp_path, {"channelId": "ch_00000001"}))
        assert out["generation"] == 2

    def test_requeue_unknown_channel_is_a_lookup_error(self, tmp_path):
        saved(tmp_path)
        with pytest.raises(LookupError):
            channel_ops.op_requeue_channel_refresh(ctx(tmp_path, {"channelId": "ch_nope"}))
        assert refresh.read_pending(tmp_path) == []

    def test_requeue_then_apply_task_drains_end_to_end(self, tmp_path):
        saved(tmp_path)
        channel_ops.op_requeue_channel_refresh(ctx(tmp_path, {"channelId": "ch_00000001"}))
        # the Apply task drains pending inline: the forced entry recomputes
        draft = copy.deepcopy(library.load(tmp_path)["channels"][0])
        draft["name"] = "Renamed only"
        result = channel_ops.op_apply_channel_changes(ctx(tmp_path, {
            "requestId": "rq-1", "expectedRevision": 1,
            "ops": json.dumps([{"op": "channel.put", "channel": draft}])}))
        assert result["status"] == "committed"
        assert result["refresh"].get("ch_00000001") == "refreshed"
        assert refresh.read_pending(tmp_path) == []
