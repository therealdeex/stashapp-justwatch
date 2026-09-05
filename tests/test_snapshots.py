"""Snapshot side-channel mechanics: retention + revision-aware publication."""

from __future__ import annotations

import json

from justwatch import snapshots


class TestSaveResultStore:
    def test_results_are_stored_per_request_id(self, tmp_path):
        data, assets = tmp_path / "data", tmp_path / "assets"
        for rid in ("req-a", "req-b"):
            snapshots.write_save_result(
                data, assets, {"requestId": rid, "saved": True, "revision": 1},
            )
        stored = {r["requestId"]: r for r in snapshots.read_save_results(data)}
        assert set(stored) == {"req-a", "req-b"}  # req-b's write did not clobber req-a

    def test_repeated_request_id_upserts(self, tmp_path):
        data, assets = tmp_path / "data", tmp_path / "assets"
        snapshots.write_save_result(data, assets, {"requestId": "req-a", "saved": False})
        snapshots.write_save_result(data, assets, {"requestId": "req-a", "saved": True})
        stored = snapshots.read_save_results(data)
        assert len(stored) == 1
        assert stored[0]["saved"] is True

    def test_retention_cap(self, tmp_path):
        data, assets = tmp_path / "data", tmp_path / "assets"
        for i in range(snapshots._SAVE_RESULT_KEEP + 5):
            snapshots.write_save_result(data, assets, {"requestId": f"req-{i}"})
        stored = snapshots.read_save_results(data)
        assert len(stored) == snapshots._SAVE_RESULT_KEEP
        assert stored[0]["requestId"] == f"req-{snapshots._SAVE_RESULT_KEEP + 4}"


class TestSnapshotPublication:
    def test_older_revision_never_replaces_newer(self, tmp_path):
        data, assets = tmp_path / "data", tmp_path / "assets"
        snapshots.write_snapshot(data, assets, {"revision": 5, "channels": {}})
        snapshots.write_snapshot(data, assets, {"revision": 3, "channels": {}})
        assert snapshots.read_snapshot(data, assets)["revision"] == 5

    def test_same_or_newer_revision_publishes(self, tmp_path):
        data, assets = tmp_path / "data", tmp_path / "assets"
        snapshots.write_snapshot(data, assets, {"revision": 5, "channels": {"a": 1}})
        snapshots.write_snapshot(data, assets, {"revision": 6, "channels": {"a": 2}})
        assert snapshots.read_snapshot(data, assets)["revision"] == 6

    def test_writes_are_atomic_unique_temps(self, tmp_path):
        data, assets = tmp_path / "data", tmp_path / "assets"
        snapshots.write_snapshot(data, assets, {"revision": 1, "channels": {}})
        leftovers = [
            p for p in (data / "snapshots").iterdir() if p.name != "directory.json"
        ]
        assert leftovers == []  # no temp files survive a write
        body = json.loads((data / "snapshots" / "directory.json").read_text())
        assert body["revision"] == 1
