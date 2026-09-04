"""Catalog validation + normalization rules."""

from __future__ import annotations

import pytest

from justwatch import catalog, contract


def channel(**overrides):
    base = {
        "id": "ch_00000001",
        "number": 1,
        "name": "Rerun Highway",
        "glyph": "\uf5e4",
        "color": "#455A64",
        "source": {"type": "tag", "id": "42"},
        "sort": "shuffle",
        "seed": 123456,
        "enabled": True,
    }
    base.update(overrides)
    return base


def codes(errors):
    return [e["code"] for e in errors]


class TestValidate:
    def test_valid_draft_has_no_errors(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1, "revision": 3,
            "settings": {},
            "channels": [channel()],
        })
        assert errors == []

    def test_every_contract_sort_is_accepted(self):
        for sort in contract.SORTS:
            errors, _ = catalog.validate({
                "schemaVersion": 1, "channels": [channel(sort=sort)],
            })
            assert errors == [], sort

    def test_duplicate_numbers_rejected(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(), channel(id="ch_00000002")],
        })
        assert "duplicate_number" in codes(errors)

    def test_number_bounds_enforced(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1, "channels": [channel(number=100)],
        })
        assert "bad_number" in codes(errors)

    def test_glyph_must_be_in_shared_set(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1, "channels": [channel(glyph="\\uf999")],
        })
        assert "unknown_glyph" in codes(errors)

    def test_unknown_source_type_rejected(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(source={"type": "group", "id": "1"})],
        })
        assert "bad_source" in codes(errors)

    def test_non_numeric_source_id_rejected(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(source={"type": "tag", "id": "abc"})],
        })
        assert "bad_source_id" in codes(errors)

    def test_saved_filter_is_a_valid_source_type(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(source={"type": "savedFilter", "id": "7"})],
        })
        assert errors == []

    def test_settings_thresholds_clamped_not_rejected(self):
        _, normalized = catalog.validate({
            "schemaVersion": 1,
            "settings": {"soloThreshold": 1, "groupThreshold": 99},
            "channels": [],
        })
        assert normalized["settings"]["soloThreshold"] >= 3
        assert normalized["settings"]["groupThreshold"] < normalized["settings"]["soloThreshold"]

    def test_bad_launch_mode_falls_back(self):
        _, normalized = catalog.validate({
            "schemaVersion": 1,
            "settings": {"launchMode": "chaos"},
            "channels": [],
        })
        assert normalized["settings"]["launchMode"] == "last"

    def test_non_object_rejected(self):
        errors, _ = catalog.validate([1, 2, 3])
        assert codes(errors) == ["not_an_object"]


class TestNormalize:
    def test_missing_seed_generated(self):
        _, normalized = catalog.validate({
            "schemaVersion": 1, "channels": [channel(seed=None)],
        })
        seeded = normalized["channels"][0]
        assert isinstance(seeded["seed"], int) and 0 <= seeded["seed"] <= catalog.MAX_SEED

    def test_channels_sorted_by_number(self):
        _, normalized = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(id="ch_00000001", number=7), channel(id="ch_00000002", number=2)],
        })
        assert [c["number"] for c in normalized["channels"]] == [2, 7]

    def test_color_lowercased_and_label_kept(self):
        _, normalized = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(color="#FFAA11", sourceLabel="Outdoor")],
        })
        stored = normalized["channels"][0]
        assert stored["color"] == "#ffaa11"
        assert stored["sourceLabel"] == "Outdoor"

    def test_roundtrip_through_disk(self, data_dir):
        draft = {"schemaVersion": 1, "revision": 5, "channels": [channel()]}
        _, normalized = catalog.validate(draft)
        normalized["revision"] = 6
        catalog.save(data_dir, normalized)
        loaded = catalog.load(data_dir)
        assert loaded == normalized

    def test_load_missing_returns_empty(self, data_dir):
        loaded = catalog.load(data_dir)
        assert loaded["revision"] == 0
        assert loaded["channels"] == []

    def test_load_corrupted_raises(self, data_dir):
        catalog.save(data_dir, {"schemaVersion": 1, "channels": []})
        catalog.catalog_path(data_dir).write_text("{not json", encoding="utf-8")
        with pytest.raises(catalog.CatalogError):
            catalog.load(data_dir)
