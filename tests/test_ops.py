"""Op dispatch: the client contract surface, through _dispatch (fake client)."""

from __future__ import annotations

import json

from justwatch import catalog, main


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
        assert mirror["requestId"] == "req-42"
        assert mirror["saved"] is True

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
        assert result["total"] == 2  # server count; Beta (no files) still counted
        assert [i["id"] for i in result["items"]] == ["10", "12"]
        assert result["items"][0]["duration"] == 1200.5
        assert result["items"][0]["studio"] == "Blue Blur"
        assert result["items"][0]["date"] == "2022-01-02"

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
        assert written["channels"]["ch_deadbeef"]["sceneCount"] == 2
