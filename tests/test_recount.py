"""The recount tool: exclusion policy transform, drift check, CSV rewrite."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

from test_networks import _importer, csv_row, write_csv

REPO = Path(__file__).resolve().parent.parent


def _recount():
    spec = importlib.util.spec_from_file_location(
        "recount_channels", REPO / "tools" / "recount_channels.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["recount_channels"] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    """Counts like the prod probes: the performer row overlaps JAV (8 scenes,
    7 of them JAV), the tag row does not (6 scenes, none JAV)."""

    def __init__(self, total=200):
        self.total = total

    def submit(self, query, variables):
        scene_filter = variables.get("scene_filter")
        if scene_filter is None:
            return {"findScenes": {"count": self.total}}
        tags = scene_filter.get("tags") or {}
        excludes_jav = "9320" in (tags.get("excludes") or [])
        if "performers" in scene_filter:
            return {"findScenes": {"count": 1 if excludes_jav else 8}}
        return {"findScenes": {"count": 6}}


def test_apply_exclusions_appends_idempotently():
    rows = [
        {"exclude_tag_ids_any": "8083", "exclude_tags_any": "PROD: Gonzo",
         "exclude_tag_logic": "ANY"},
        {"exclude_tag_ids_any": "", "exclude_tags_any": "",
         "exclude_tag_logic": "ANY"},
    ]
    assert _recount().apply_exclusions(rows, [("9320", "JAV")]) == 2
    assert rows[0]["exclude_tag_ids_any"] == "8083|9320"
    assert rows[0]["exclude_tags_any"] == "PROD: Gonzo|JAV"
    assert rows[1]["exclude_tag_ids_any"] == "9320"
    assert rows[1]["exclude_tags_any"] == "JAV"
    assert all(row["exclude_tag_logic"] == "ANY" for row in rows)
    assert _recount().apply_exclusions(rows, [("9320", "JAV")]) == 0


def test_transformed_row_imports_with_both_excludes():
    row = csv_row(
        channel_number="700", channel_name="Slow Grind, Hold the No Script",
        channel_family="tag_include_exclude", exact_scene_count="437",
        include_tag_logic="ALL", include_tag_ids_all="7992",
        include_tags_all="ACT: Grinding",
        exclude_tag_ids_any="8083", exclude_tags_any="PROD: Gonzo",
    )
    _recount().apply_exclusions([row], [("9320", "JAV")])
    importer = _importer()
    assert importer.build_source(row) == {
        "type": "filter", "tags": ["7992"], "excludeTags": ["8083", "9320"],
    }
    assert importer.source_label(row) == "ACT: Grinding (without PROD: Gonzo, JAV)"


def test_main_check_write_roundtrip(tmp_path, monkeypatch, capsys):
    recount = _recount()
    rows = [
        csv_row(
            channel_number="100", channel_name="Perf",
            channel_family="performer_spotlight", exact_scene_count="8",
            library_share_pct="4.000",
            include_performer_logic="ALL", include_performer_ids_all="157",
            include_performers_all="Mick Blue",
        ),
        csv_row(
            channel_number="700", channel_name="Tags",
            channel_family="tag_include_exclude", exact_scene_count="6",
            library_share_pct="3.000",
            include_tag_logic="ALL", include_tag_ids_all="7992",
            include_tags_all="ACT: Grinding",
            exclude_tag_ids_any="8083", exclude_tags_any="PROD: Gonzo",
        ),
    ]
    path = write_csv(tmp_path / "channels.csv", rows)
    key_file = tmp_path / "key"
    key_file.write_text("test-key", encoding="utf-8")
    client = FakeClient(total=200)
    monkeypatch.setattr(recount, "build_client", lambda url, api_key: client)
    base = ["--csv", str(path), "--url", "http://stash:9999",
            "--api-key-file", str(key_file)]

    # The stored CSV matches the live library before any policy.
    assert recount.main([*base, "--check"]) == 0
    # Applying the policy drifts row 100 (8 -> 1); --check fails and writes nothing.
    before = path.read_text(encoding="utf-8-sig")
    assert recount.main([*base, "--exclude-tag", "9320=JAV", "--check"]) == 1
    assert path.read_text(encoding="utf-8-sig") == before
    # --write persists the exclusion on BOTH rows (row 700's count does not
    # drift, but its criteria changed) and the recounted share.
    assert recount.main([*base, "--exclude-tag", "9320=JAV", "--write"]) == 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        out = list(csv.DictReader(handle))
    assert out[0]["exclude_tag_ids_any"] == "9320"
    assert out[0]["exact_scene_count"] == "1"
    assert out[0]["library_share_pct"] == "0.500"
    assert out[1]["exclude_tag_ids_any"] == "8083|9320"
    assert out[1]["exact_scene_count"] == "6"
    # The sweep is clean against the rewritten CSV, drift and review reported.
    assert recount.main([*base, "--check"]) == 0
    captured = capsys.readouterr()
    assert "100 Perf: 8 -> 1" in captured.out
    assert "REVIEW row 100" in captured.out
