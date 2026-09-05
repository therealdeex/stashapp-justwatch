"""Catalog validation + normalization rules."""

from __future__ import annotations

import json

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


class TestTagSetSources:
    """A tag channel may air from a SET of tags (source.ids = ANY-of union).
    Canonical form keeps ``id`` mirroring the first sorted id so stored files
    stay strictly loadable and hashes/equality stay order-stable."""

    def test_ids_only_draft_is_valid_and_canonicalized(self):
        errors, normalized = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(source={"type": "tag", "ids": ["9", "3", "12"]})],
        })
        assert errors == []
        assert normalized["channels"][0]["source"] == {"type": "tag", "id": "3", "ids": ["3", "9", "12"]}

    def test_id_and_ids_merge_sorted_deduped(self):
        _, normalized = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(source={"type": "tag", "id": "9", "ids": ["12", "3", "9", "3"]})],
        })
        assert normalized["channels"][0]["source"]["ids"] == ["3", "9", "12"]
        assert normalized["channels"][0]["source"]["id"] == "3"

    def test_single_tag_canonicalizes_to_a_set_of_one(self):
        _, normalized = catalog.validate({
            "schemaVersion": 1, "channels": [channel()],
        })
        assert normalized["channels"][0]["source"] == {"type": "tag", "id": "42", "ids": ["42"]}

    def test_non_numeric_ids_rejected(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(source={"type": "tag", "id": "3", "ids": ["3", "oops"]})],
        })
        assert "bad_source_ids" in codes(errors)

    def test_empty_ids_list_rejected(self):
        errors, _ = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(source={"type": "tag", "id": "3", "ids": []})],
        })
        assert "bad_source_ids" in codes(errors)

    def test_non_tag_sources_stay_single_id(self):
        _, normalized = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(source={"type": "savedFilter", "id": "7", "ids": ["3"]})],
        })
        assert normalized["channels"][0]["source"] == {"type": "savedFilter", "id": "7"}

    def test_stored_ids_only_source_loads(self, data_dir):
        path = catalog.catalog_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schemaVersion": 1, "revision": 1,
            "channels": [channel(source={"type": "tag", "ids": ["7", "3"]})],
        }), encoding="utf-8")
        loaded = catalog.load(data_dir)
        assert loaded["channels"][0]["source"] == {"type": "tag", "id": "3", "ids": ["3", "7"]}

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

    def test_malformed_fields_are_structured_errors_not_crashes(self):
        errors, normalized = catalog.validate({
            "schemaVersion": 1,
            "channels": [channel(id="ch_00000001", name=42, source="tag")],
            "settings": {"includeTags": {"general": None}},
        })
        assert "bad_name" in codes(errors)
        assert "bad_source" in codes(errors)
        # normalization survived the garbage and echoed a best-effort shape
        assert normalized["channels"][0]["name"] == "42"
        assert normalized["settings"]["includeTags"]["general"] == []

    def test_null_tag_list_does_not_crash(self):
        errors, normalized = catalog.validate({
            "schemaVersion": 1,
            "settings": {"includeTags": {"general": None}},
            "channels": [],
        })
        assert errors == []
        assert normalized["settings"]["includeTags"]["general"] == []


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


class TestStrictLoad:
    """A structurally broken stored catalog must never load as editable — the
    next save would silently make the loss permanent."""

    def stored(self, data_dir, body):
        path = catalog.catalog_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body) if not isinstance(body, str) else body,
                        encoding="utf-8")
        return path

    def test_broken_channels_value_never_loads_as_empty(self, data_dir):
        self.stored(data_dir, {"schemaVersion": 1, "revision": 5, "channels": "broken"})
        with pytest.raises(catalog.CatalogError, match="channels"):
            catalog.load(data_dir)

    def test_future_schema_is_rejected_not_downgraded(self, data_dir):
        self.stored(data_dir, {"schemaVersion": 99, "revision": 1, "channels": []})
        with pytest.raises(catalog.CatalogError, match="schemaVersion"):
            catalog.load(data_dir)

    def test_non_object_channel_member_is_rejected(self, data_dir):
        self.stored(data_dir, {"schemaVersion": 1, "revision": 1, "channels": ["nope"]})
        with pytest.raises(catalog.CatalogError, match=r"channels\[0\]"):
            catalog.load(data_dir)

    def test_channel_missing_seed_is_rejected(self, data_dir):
        ch = channel()
        del ch["seed"]
        self.stored(data_dir, {"schemaVersion": 1, "revision": 1, "channels": [ch]})
        with pytest.raises(catalog.CatalogError, match="seed"):
            catalog.load(data_dir)

    def test_original_bytes_are_preserved(self, data_dir):
        broken = '{"schemaVersion":1,"revision":5,"channels":"broken"}'
        path = self.stored(data_dir, broken)
        with pytest.raises(catalog.CatalogError):
            catalog.load(data_dir)
        assert path.read_text(encoding="utf-8") == broken

    def test_a_valid_stored_catalog_round_trips(self, data_dir):
        _, normalized = catalog.validate({
            "schemaVersion": 1, "revision": 2,
            "settings": {}, "channels": [channel()],
        })
        normalized["revision"] = 3
        catalog.save(data_dir, normalized)
        assert catalog.load(data_dir) == normalized

    def test_concurrent_saves_use_distinct_temp_files(self, data_dir):
        """Two writers must never share a temp path (regression for the shared
        catalog.json.tmp collision)."""
        _, normalized = catalog.validate({"schemaVersion": 1, "channels": [channel()]})
        import threading

        seen = []
        original_replace = catalog.os.replace

        def spy_replace(src, dst):
            seen.append(str(src))
            return original_replace(src, dst)

        catalog.os.replace = spy_replace
        try:
            threads = [
                threading.Thread(target=catalog.save, args=(data_dir, dict(normalized)))
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            catalog.os.replace = original_replace
        assert len(set(seen)) == 4  # every write had its own temp file
