"""Op dispatch: the client contract surface, through _dispatch (fake client)."""

from __future__ import annotations

import json

from justwatch import catalog, continuing, main, networks


def dispatch(envelope: dict, args: dict, fake_client) -> dict:
    envelope = dict(envelope)
    envelope["args"] = args
    return main._dispatch(envelope, client=fake_client)


def draft(revision=0, **channel_overrides):
    channel = {
        "id": "ch_deadbeef",
        "number": 1,
        "name": "Outdoor Hour",
        "glyph": "\uf1b0",
        "color": "#388E3C",
        "source": {"type": "tag", "id": "42"},
        "sort": "shuffle",
        "seed": 99,
        "enabled": True,
    }
    channel.update(channel_overrides)
    return {
        "schemaVersion": 1,
        "revision": revision,
        "settings": {},
        "channels": [channel],
    }


class TestCapabilities:
    def test_handshake_shape(self, envelope, fake_client):
        caps = dispatch(envelope, {"mode": "Capabilities"}, fake_client)
        assert caps["pluginId"] == "stash-justwatch"
        assert caps["contractVersion"] == 1
        assert caps["operations"]["lineup"] == "Lineup"
        assert caps["features"]["channelNumbers"] == [1, 99]
        assert "shuffle" in caps["features"]["sorts"]
        assert caps["limits"]["lineupPerPage"] == 50
        assert caps["features"]["rotation"]["size"] == 50


class TestSaveCatalog:
    def test_save_persists_and_bumps_revision(self, envelope, data_dir, fake_client):
        result = dispatch(
            envelope,
            {"mode": "SaveCatalog", "catalog": json.dumps(draft(revision=0)),
             "expectedRevision": "0", "requestId": "req-1"},
            fake_client,
        )
        assert result["saved"] is True
        assert result["revision"] == 1
        stored = catalog.load(data_dir)
        assert stored["channels"][0]["sourceLabel"] == "Outdoor"

    def test_save_writes_result_mirror_with_request_id(
        self, envelope, assets_dir, fake_client,
    ):
        dispatch(
            envelope,
            {"mode": "SaveCatalog", "catalog": json.dumps(draft()),
             "expectedRevision": "0", "requestId": "req-42"},
            fake_client,
        )
        mirror = json.loads(
            (assets_dir / "snapshots" / "save_result.json").read_text(encoding="utf-8"),
        )
        entry = next(r for r in mirror["results"] if r["requestId"] == "req-42")
        assert entry["saved"] is True

    def test_each_save_keeps_its_own_result(self, envelope, assets_dir, fake_client):
        dispatch(envelope, {"mode": "SaveCatalog", "catalog": json.dumps(draft()),
                            "expectedRevision": "0", "requestId": "req-a"}, fake_client)
        draft2 = draft(revision=1)
        draft2["channels"][0]["name"] = "Renamed"
        dispatch(envelope, {"mode": "SaveCatalog", "catalog": json.dumps(draft2),
                            "expectedRevision": "1", "requestId": "req-b"}, fake_client)
        mirror = json.loads(
            (assets_dir / "snapshots" / "save_result.json").read_text(encoding="utf-8"),
        )
        by_id = {r["requestId"]: r for r in mirror["results"]}
        assert by_id["req-a"]["saved"] is True and by_id["req-a"]["revision"] == 1
        assert by_id["req-b"]["saved"] is True and by_id["req-b"]["revision"] == 2

    def test_internal_failure_still_publishes_result(
        self, envelope, data_dir, fake_client,
    ):
        from pathlib import Path

        from justwatch import snapshots

        def broken_load(data_dir):
            raise catalog.CatalogError("boom")

        original = catalog.load
        catalog.load = broken_load
        try:
            result = dispatch(
                envelope,
                {"mode": "SaveCatalog", "catalog": json.dumps(draft()),
                 "expectedRevision": "0", "requestId": "req-x"},
                fake_client,
            )
        finally:
            catalog.load = original
        assert result["error"] in ("catalog_corrupt", "internal_error")
        # The failure was mirrored so the editor's poll finds its own result.
        stored = snapshots.read_save_results(
            Path(envelope["server_connection"]["Dir"]) / catalog.DATA_DIR_NAME,
        )
        match = [r for r in stored if r["requestId"] == "req-x"]
        assert match and match[0]["error"] == result["error"]

    def test_seed_of_existing_channel_is_immutable(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, draft())  # stored seed 99, revision 0
        changed = json.dumps(draft(seed=12345))
        result = dispatch(envelope, {"mode": "SaveCatalog", "catalog": changed,
                                     "expectedRevision": "0"}, fake_client)
        assert result["saved"] is True
        assert catalog.load(data_dir)["channels"][0]["seed"] == 99

    def test_omitted_seed_keeps_stored_seed(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, draft())  # stored seed 99, revision 0
        changed = draft()
        del changed["channels"][0]["seed"]
        result = dispatch(envelope, {"mode": "SaveCatalog", "catalog": json.dumps(changed),
                                     "expectedRevision": "0"}, fake_client)
        assert result["saved"] is True
        assert catalog.load(data_dir)["channels"][0]["seed"] == 99

    def test_new_channel_gets_its_own_seed(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, draft())
        two = draft()
        two["channels"].append(dict(two["channels"][0], id="ch_000000ff", number=2, seed=777))
        result = dispatch(envelope, {"mode": "SaveCatalog", "catalog": json.dumps(two),
                                     "expectedRevision": "0"}, fake_client)
        assert result["saved"] is True
        stored = {c["id"]: c["seed"] for c in catalog.load(data_dir)["channels"]}
        assert stored["ch_deadbeef"] == 99
        assert stored["ch_000000ff"] == 777

    def test_corrupt_catalog_blocks_save_and_preserves_file(
        self, envelope, data_dir, fake_client,
    ):
        path = catalog.catalog_path(data_dir)
        path.parent.mkdir(parents=True)
        broken = '{"schemaVersion":1,"revision":5,"channels":"broken"}'
        path.write_text(broken, encoding="utf-8")
        result = dispatch(
            envelope,
            {"mode": "SaveCatalog", "catalog": json.dumps(draft()),
             "expectedRevision": "5", "requestId": "req-9"},
            fake_client,
        )
        assert result["saved"] is False
        assert result["error"] == "catalog_corrupt"
        assert path.read_text(encoding="utf-8") == broken

    def test_concurrent_saves_yield_one_success_one_conflict(
        self, envelope, data_dir, fake_client,
    ):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        catalog.save(data_dir, draft())
        gate = threading.Barrier(2)

        def slow_submit(query, variables=None):
            # Stretch the window between the revision check and the write.
            gate.wait(timeout=5)
            for marker, payload in fake_client.responses.items():
                if marker in query:
                    return json.loads(json.dumps(payload))
            raise AssertionError(f"unexpected query: {query[:60]}")

        fake_client.submit = slow_submit
        results = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                tag: pool.submit(
                    dispatch, envelope,
                    {"mode": "SaveCatalog", "catalog": json.dumps(draft()),
                     "expectedRevision": "0", "requestId": f"req-{tag}"},
                    fake_client,
                )
                for tag in ("a", "b")
            }
            for tag, fut in futures.items():
                results[tag] = fut.result(timeout=30)

        outcomes = sorted(r.get("error", "saved") for r in results.values())
        assert outcomes == ["revision_conflict", "saved"]
        stored = catalog.load(data_dir)
        assert stored["revision"] == 1  # no lost write, no double increment

    def test_revision_conflict_rejected(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, {"schemaVersion": 1, "revision": 4, "channels": []})
        result = dispatch(
            envelope,
            {"mode": "SaveCatalog", "catalog": json.dumps(draft(revision=4)),
             "expectedRevision": "0"},
            fake_client,
        )
        assert result["saved"] is False
        assert result["error"] == "revision_conflict"
        assert result["currentRevision"] == 4

    def test_validation_errors_block_save(self, envelope, data_dir, fake_client):
        result = dispatch(
            envelope,
            {"mode": "SaveCatalog", "catalog": json.dumps(draft(number=500)),
             "expectedRevision": "0"},
            fake_client,
        )
        assert result["saved"] is False
        assert result["error"] == "validation_failed"
        assert catalog.load(data_dir)["channels"] == []

    def test_transport_failure_fails_save_without_marking_missing(
        self, envelope, fake_client,
    ):
        from justwatch.stash_client import GraphQLClientError

        def reject(query, variables=None):
            raise GraphQLClientError("not found")

        fake_client.submit = reject
        result = dispatch(
            envelope,
            {"mode": "SaveCatalog", "catalog": json.dumps(draft()),
             "expectedRevision": "0"},
            fake_client,
        )
        assert result["saved"] is False
        assert result["error"] == "source_check_failed"

    def test_task_page_run_without_draft_is_noop(self, envelope, fake_client):
        result = dispatch(envelope, {"mode": "SaveCatalog"}, fake_client)
        assert result["saved"] is False
        assert "no draft" in result["message"]


class TestLineup:
    def test_lineup_returns_playable_items(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, draft())
        result = dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
        assert result["channelId"] == "ch_deadbeef"
        assert result["total"] == 2  # playable rotation: Beta (no files) skipped
        assert result["sourceTotal"] == 2  # the server's raw count
        assert [i["id"] for i in result["items"]] == ["10", "12"]
        assert result["items"][0]["duration"] == 1200.5
        assert result["items"][0]["studio"] == "Blue Blur"
        assert result["items"][0]["date"] == "2022-01-02"
        assert result["rotationVersion"].startswith("r0-")
        assert result["loopSeconds"] == 1500.5

    def test_lineup_uses_seeded_random_sort(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, draft())
        dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
        _, variables = fake_client.calls[-1]
        assert variables["filter"]["sort"] == "random_99"
        assert variables["filter"]["per_page"] == 50

    def test_lineup_tag_filter_is_hierarchical_include(
        self, envelope, data_dir, fake_client,
    ):
        catalog.save(data_dir, draft())
        dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
        _, variables = fake_client.calls[-1]
        assert variables["scene_filter"] == {
            "tags": {"value": ["42"], "modifier": "INCLUDES", "depth": -1},
        }

    def test_rotation_scans_past_unplayable_head(self, envelope, data_dir, fake_client):
        """Unplayable scenes early in the order must not hollow out the channel:
        the scan walks pages until the rotation is filled or the source ends."""
        catalog.save(data_dir, draft())

        pages = {
            1: {"count": 60, "scenes": [
                {"id": str(i), "title": f"dead{i}", "files": []} for i in range(50)
            ]},
            2: {"count": 60, "scenes": [
                {"id": str(50 + i), "title": f"live{i}", "files": [{"duration": 600.0}]}
                for i in range(10)
            ]},
        }

        def paged_submit(query, variables=None):
            assert "JustWatchLineup" in query
            return {"findScenes": pages[variables["filter"]["page"]]}

        fake_client.submit = paged_submit
        result = dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
        assert [i["id"] for i in result["items"]] == [str(50 + i) for i in range(10)]
        assert result["sourceTotal"] == 60
        assert result["rotationComplete"] is True
        assert result["loopSeconds"] == 6000.0

    def test_rotation_capped_at_size(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, draft())

        def big_submit(query, variables=None):
            assert "JustWatchLineup" in query
            page = variables["filter"]["page"]
            return {"findScenes": {"count": 500, "scenes": [
                {"id": str(page * 1000 + i), "files": [{"duration": 60.0}]}
                for i in range(100)
            ]}}

        fake_client.submit = big_submit
        result = dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
        assert result["total"] == 50
        assert result["sourceTotal"] == 500
        assert result["rotationComplete"] is False  # loop is a sampled subset
        assert result["loopSeconds"] == 3000.0

    def test_unknown_or_disabled_channel_is_an_error(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, draft(enabled=False))
        try:
            dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
            raise AssertionError("expected LookupError")
        except LookupError:
            pass

    def test_saved_filter_lineup_uses_object_filter(
        self, envelope, data_dir, fake_client,
    ):
        catalog.save(data_dir, draft(source={"type": "savedFilter", "id": "7"}, sort="top_rated"))
        result = dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
        _, variables = fake_client.calls[-1]
        assert variables["scene_filter"] == {"rating100": {"value": 4, "modifier": "GREATER_THAN"}}
        assert variables["filter"]["sort"] == "rating"
        assert result["total"] == 2

    def test_saved_filter_text_search_is_carried_through(self, envelope, data_dir, fake_client):
        """A text-only saved search must not become an unrestricted lineup."""
        conftest = fake_client.responses
        conftest["JustWatchSavedFilter"]["findSavedFilter"] = {
            "id": "7", "name": "Bluey", "mode": "SCENES",
            "find_filter": {"q": "Bluey"},
            "object_filter": None,
        }
        catalog.save(data_dir, draft(source={"type": "savedFilter", "id": "7"}))
        result = dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
        _, variables = fake_client.calls[-1]
        assert variables["filter"]["q"] == "Bluey"
        assert variables["scene_filter"] == {}
        assert result["total"] == 2

    def test_missing_saved_filter_is_an_error(self, envelope, data_dir, fake_client):
        catalog.save(data_dir, draft(source={"type": "savedFilter", "id": "7"}))
        fake_client.responses["JustWatchSavedFilter"]["findSavedFilter"]["mode"] = "MARKERS"
        try:
            dispatch(envelope, {"mode": "Lineup", "channelId": "ch_deadbeef"}, fake_client)
            raise AssertionError("expected LookupError")
        except LookupError:
            pass


class TestPreviewLineup:
    def test_draft_preview_includes_paths(self, envelope, fake_client):
        result = dispatch(
            envelope,
            {"mode": "PreviewLineup", "channel": json.dumps(draft()["channels"][0])},
            fake_client,
        )
        assert result["items"][0]["preview"] == "/scene/10/preview"
        assert result["perPage"] == 12


class TestDirectory:
    def test_directory_hides_disabled_and_merges_health(
        self, envelope, data_dir, assets_dir, fake_client,
    ):
        stored = draft()
        stored["channels"].append(dict(
            stored["channels"][0], id="ch_00000002", number=2, enabled=False,
        ))
        catalog.save(data_dir, stored)
        snapshots_payload = {
            "revision": 0,
            "channels": {"ch_deadbeef": {
                "sceneCount": 412, "loopSeconds": 32400.0,
                "loopCapped": False, "sourceMissing": False,
            }},
        }
        (assets_dir / "snapshots").mkdir(parents=True)
        (assets_dir / "snapshots" / "directory.json").write_text(
            json.dumps(snapshots_payload), encoding="utf-8",
        )
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        assert [c["id"] for c in result["channels"]] == ["ch_deadbeef"]
        assert result["channels"][0]["sceneCount"] == 412

    def test_stale_snapshot_health_not_merged(self, envelope, data_dir, assets_dir, fake_client):
        catalog.save(data_dir, draft())
        snapshots_payload = {"revision": 99, "channels": {"ch_deadbeef": {"sceneCount": 1}}}
        (assets_dir / "snapshots").mkdir(parents=True)
        (assets_dir / "snapshots" / "directory.json").write_text(
            json.dumps(snapshots_payload), encoding="utf-8",
        )
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        assert result["channels"][0]["sceneCount"] is None


class TestRefreshData:
    def test_refresh_writes_snapshot(self, envelope, data_dir, assets_dir, fake_client):
        catalog.save(data_dir, draft())
        result = dispatch(envelope, {"mode": "RefreshData"}, fake_client)
        assert result["channels"] == 1
        written = json.loads(
            (assets_dir / "snapshots" / "directory.json").read_text(encoding="utf-8"),
        )
        entry = written["channels"]["ch_deadbeef"]
        assert entry["sceneCount"] == 2
        assert entry["healthStatus"] == "ok"
        assert entry["rotationVersion"]


class TestHealth:
    """Health distinguishes off air, missing source, and temporary unavailability."""

    def _refresh(self, envelope, assets_dir, fake_client):
        dispatch(envelope, {"mode": "RefreshData"}, fake_client)
        return json.loads(
            (assets_dir / "snapshots" / "directory.json").read_text(encoding="utf-8"),
        )

    def test_empty_rotation_with_live_source_is_off_air(
        self, envelope, data_dir, assets_dir, fake_client,
    ):
        catalog.save(data_dir, draft())
        fake_client.responses["JustWatchLineup"] = {"findScenes": {"count": 0, "scenes": []}}
        entry = self._refresh(envelope, assets_dir, fake_client)["channels"]["ch_deadbeef"]
        assert entry["healthStatus"] == "offAir"
        assert entry["sourceMissing"] is False
        assert entry["sceneCount"] == 0

    def test_deleted_entity_is_missing_source(
        self, envelope, data_dir, assets_dir, fake_client,
    ):
        catalog.save(data_dir, draft(source={"type": "performer", "id": "55"}))
        fake_client.responses["JustWatchLineup"] = {"findScenes": {"count": 0, "scenes": []}}
        # conftest's findPerformer is None → the entity no longer exists.
        entry = self._refresh(envelope, assets_dir, fake_client)["channels"]["ch_deadbeef"]
        assert entry["healthStatus"] == "missingSource"
        assert entry["sourceMissing"] is True

    def test_transport_failure_keeps_stale_numbers_marked_unavailable(
        self, envelope, data_dir, assets_dir, fake_client,
    ):
        from justwatch.stash_client import GraphQLClientError

        catalog.save(data_dir, draft())
        (assets_dir / "snapshots").mkdir(parents=True)
        (assets_dir / "snapshots" / "directory.json").write_text(json.dumps({
            "revision": 0,
            "channels": {"ch_deadbeef": {
                "healthStatus": "ok", "sceneCount": 7, "loopSeconds": 100.0,
                "loopCapped": False, "sourceMissing": False,
                "sourceTotal": 7, "rotationVersion": "old",
            }},
        }), encoding="utf-8")

        def down(query, variables=None):
            raise GraphQLClientError("server unreachable")

        fake_client.submit = down
        entry = self._refresh(envelope, assets_dir, fake_client)["channels"]["ch_deadbeef"]
        assert entry["healthStatus"] == "unavailable"
        assert entry["sceneCount"] == 7  # last known numbers survive
        assert entry["rotationVersion"] == "old"

    def test_one_broken_channel_does_not_abort_the_rest(
        self, envelope, data_dir, assets_dir, fake_client,
    ):
        from justwatch.stash_client import GraphQLClientError

        stored = draft()
        stored["channels"].append(dict(
            stored["channels"][0], id="ch_00000002", number=2,
            source={"type": "tag", "id": "43"},
        ))
        catalog.save(data_dir, stored)

        def selective(query, variables=None):
            scene_filter = variables.get("scene_filter") or {}
            if (scene_filter.get("tags") or {}).get("value") == ["43"]:
                raise GraphQLClientError("kaboom")
            for marker, payload in fake_client.responses.items():
                if marker in query:
                    return json.loads(json.dumps(payload))
            raise AssertionError(f"unexpected query: {query[:60]}")

        fake_client.submit = selective
        channels = self._refresh(envelope, assets_dir, fake_client)["channels"]
        assert channels["ch_deadbeef"]["healthStatus"] == "ok"
        assert channels["ch_00000002"]["healthStatus"] == "unavailable"


class TestContinuingNetworks:
    """The continuing-programming op surface: rollout-gated Schedule/Directory,
    the status manifest, capabilities, and the prepare task."""

    NET = "net_00000001"

    def _networks(self, tmp, monkeypatch, *, pin=False):
        from justwatch import networks as net_mod
        rows = [{
            "id": self.NET, "number": 100, "name": "Pilot One", "glyph": "\uf005",
            "color": "#123456", "section": "general", "family": "tag_spotlight",
            "count": 12, "sort": "shuffle", "seed": 17, "programmingMode": "fixed",
            "source": {"type": "filter", "tags": ["42"]},
        }]
        if pin:
            rows[0]["programming"] = {"mode": "fixed"}
        monkeypatch.setattr(net_mod, "PATH", tmp / "networks.json")
        net_mod.PATH.write_text(json.dumps({"revision": "r", "channels": rows}))

    def _rollout(self, data_dir, enabled=True):
        (data_dir).mkdir(parents=True, exist_ok=True)
        (data_dir / "continuing-networks.json").write_text(
            json.dumps({"enabled": enabled, "networkIds": [self.NET]}))

    def _index_client(self, n=12):
        from justwatch.stash_client import GraphQLClientError

        class Index:
            def submit(self, query, variables=None):
                if "ContinuingIndex" in query:
                    return {"findScenes": {"count": n, "scenes": [
                        {"id": str(i), "title": f"S{i}", "date": None, "created_at": None,
                         "studio": {"id": "1", "name": "St"}, "performers": [],
                         "files": [{"duration": 600}], "paths": {"preview": "/p"}}
                        for i in range(n)]}}
                raise GraphQLClientError(f"unexpected query {query[:40]}")

        return Index()

    def test_capabilities_advertise_network_programming(self, envelope, fake_client):
        caps = dispatch(envelope, {"mode": "Capabilities"}, fake_client)
        feature = caps["features"]["programming"]
        assert feature["networks"] is True
        assert feature["horizonHours"] == 168 and feature["protectedHours"] == 24
        assert caps["operations"]["programmingStatus"] == "ProgrammingStatus"

    def test_schedule_for_fixed_network_is_fixed(self, envelope, data_dir, fake_client, tmp_path, monkeypatch):
        self._networks(tmp_path, monkeypatch)  # no rollout file
        result = dispatch(envelope, {"mode": "Schedule", "channelId": self.NET}, fake_client)
        assert result == {"status": "fixed", "programs": []}

    def test_schedule_serves_activated_network_publication(
        self, envelope, data_dir, fake_client, tmp_path, monkeypatch,
    ):
        from justwatch import programming as prog, snapshots
        self._networks(tmp_path, monkeypatch)
        self._rollout(data_dir)
        publication = continuing.build(
            self._channel(), [{"id": str(i), "title": f"S{i}", "duration": 600,
                               "studioId": "1", "studio": "St", "performerIds": [],
                               "date": "", "createdAt": "", "preview": ""} for i in range(5)],
            None, now=0)
        snapshots.write_json(prog.path(data_dir, self.NET), publication)
        page = dispatch(envelope, {"mode": "Schedule", "channelId": self.NET, "at": "0"}, fake_client)
        assert page["status"] == "ready" and len(page["programs"]) == 50
        assert page["channelId"] == self.NET
        assert page["programs"][0]["startEpochMs"] == 0

    def test_directory_overlays_resolved_mode_and_status(
        self, envelope, data_dir, fake_client, tmp_path, monkeypatch,
    ):
        from justwatch import programming as prog, snapshots
        self._networks(tmp_path, monkeypatch)
        self._rollout(data_dir)
        # Live status (read-time coverage, R7): the publication must cover the
        # reader's wall clock for the directory to advertise it as ready.
        now = int(__import__("time").time() * 1000)
        publication = continuing.build(self._channel(),
            [{"id": str(i), "title": f"S{i}", "duration": 600,
              "studioId": "1", "studio": "St", "performerIds": [],
              "date": "", "createdAt": "", "preview": ""} for i in range(5)],
            None, now=now)
        snapshots.write_json(prog.path(data_dir, self.NET), publication)
        continuing.write_status(data_dir, {"runId": "t", "networkChannels": {self.NET: "ready"}}, now=now)
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        row = next(r for r in result["networks"]["channels"] if r["id"] == self.NET)
        assert row["programmingMode"] == "continuing"
        assert row["schedule"]["coverageHours"] >= 0

    def test_directory_without_rollout_keeps_compiled_modes(
        self, envelope, data_dir, fake_client, tmp_path, monkeypatch,
    ):
        self._networks(tmp_path, monkeypatch)
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        row = next(r for r in result["networks"]["channels"] if r["id"] == self.NET)
        assert row["programmingMode"] == "fixed" and "schedule" not in row
        # revision is the compiled content identity (recomputed on load), and
        # runtime status overlays never churn it.
        assert result["networks"]["revision"] == networks.revision(result["networks"]["channels"])

    def test_authored_fixed_pin_beats_rollout(
        self, envelope, data_dir, fake_client, tmp_path, monkeypatch,
    ):
        self._networks(tmp_path, monkeypatch, pin=True)
        self._rollout(data_dir)
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        row = next(r for r in result["networks"]["channels"] if r["id"] == self.NET)
        assert row["programmingMode"] == "fixed"

    def test_disabled_rollout_deactivates(self, envelope, data_dir, fake_client, tmp_path, monkeypatch):
        self._networks(tmp_path, monkeypatch)
        self._rollout(data_dir, enabled=False)
        result = dispatch(envelope, {"mode": "Schedule", "channelId": self.NET}, fake_client)
        assert result["status"] == "fixed"

    def test_prepare_task_publishes_network_and_status(
        self, envelope, data_dir, tmp_path, monkeypatch,
    ):
        from justwatch import catalog as cat
        self._networks(tmp_path, monkeypatch)
        self._rollout(data_dir)
        cat.save(data_dir, cat.empty())
        monkeypatch.setattr(
            "justwatch.main._HANDLERS", dict(main._HANDLERS))  # isolation no-op
        result = main._dispatch({**envelope, "args": {"mode": "PrepareProgramming"},
                                 "server_connection": {**envelope["server_connection"],
                                                       "Dir": str(data_dir.parent)}},
                                client=self._index_client())
        assert result["networks"][self.NET] == "ready"
        status = continuing.read_status(data_dir)
        assert status["channels"][self.NET]["mode"] == "continuing"
        assert status["lastRun"]["networkChannels"][self.NET] == "ready"

    def test_programming_status_op_reads_manifest(self, envelope, data_dir, fake_client):
        continuing.write_status(data_dir, {"networkChannels": {}}, now=123)
        result = dispatch(envelope, {"mode": "ProgrammingStatus"}, fake_client)
        assert result["generatedAt"] == 123

    def test_desk_includes_bounded_network_section(self, envelope, data_dir, fake_client, tmp_path, monkeypatch):
        self._networks(tmp_path, monkeypatch)
        self._rollout(data_dir)
        continuing.write_status(data_dir, {"networkChannels": {self.NET: "ready"}}, now=0)
        result = dispatch(envelope, {"mode": "ProgrammingDesk"}, fake_client)
        assert result["networks"]["rollout"]["enabled"] is True
        assert result["networks"]["total"] == 1
        assert result["networks"]["channels"][0]["id"] == self.NET

    def _channel(self):
        return {"id": self.NET, "number": 100, "name": "Pilot One", "glyph": "\uf005",
                "color": "#123456", "section": "general", "family": "tag_spotlight",
                "count": 12, "sort": "shuffle", "seed": 17, "programmingMode": "fixed",
                "source": {"type": "filter", "tags": ["42"]}}
