"""The network tier: importer, strict loader, filter projection, op wiring."""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from justwatch import contract, lineup, networks
from justwatch.networks import NetworksError

from test_ops import dispatch


REPO = Path(__file__).resolve().parent.parent


def _importer():
    spec = importlib.util.spec_from_file_location(
        "import_channels", REPO / "tools" / "import_channels.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_channels"] = module
    spec.loader.exec_module(module)
    return module


def csv_row(**overrides) -> dict:
    row = {key: "" for key in (
        "channel_number", "channel_name", "channel_family", "exact_scene_count",
        "include_tag_logic", "include_tag_ids_all", "include_tags_all",
        "exclude_tag_logic", "exclude_tag_ids_any", "exclude_tags_any",
        "include_performer_logic", "include_performer_ids_all", "include_performers_all",
        "include_studio_logic", "include_studio_ids_any", "include_studios_any",
        "rationale",
        # v0.6 additions — all optional; blank keeps legacy behavior
        "stable_key", "scene_date_from", "scene_date_to",
        "duration_min_seconds", "created_within_days",
    )}
    row.update(overrides)
    return row


def write_csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def networks_file(tmp_path: Path, channels: list[dict]) -> Path:
    path = tmp_path / "networks.json"
    path.write_text(json.dumps({"channels": channels}), encoding="utf-8")
    return path


def net_channel(**overrides) -> dict:
    channel = {
        "id": "net_738dc8a1",
        "number": 100,
        "name": "Mick Blue After Dark",
        "glyph": "\uf007",
        "color": "#8E24AA",
        "section": "performers",
        "family": "performer_spotlight",
        "count": 573,
        "sort": "shuffle",
        "seed": 123456,
        "programmingMode": "fixed",
        "sourceLabel": "Mick Blue",
        "source": {"type": "filter", "performers": ["157"]},
    }
    channel.update(overrides)
    return channel


@pytest.fixture
def net_path(tmp_path, monkeypatch):
    """Point the loader at a scratch networks file."""
    path = tmp_path / "networks.json"
    monkeypatch.setattr(networks, "PATH", path)
    return path


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class TestImporter:
    def test_import_maps_families_and_is_deterministic(self, tmp_path):
        importer = _importer()
        rows = [
            csv_row(channel_number="100", channel_name="Mick Blue After Dark",
                    channel_family="performer_spotlight", exact_scene_count="573",
                    include_performer_logic="ALL", include_performer_ids_all="157",
                    include_performers_all="Mick Blue",
                    rationale="Performer spotlight."),
            csv_row(channel_number="700", channel_name="Slow Grind",
                    channel_family="tag_include_exclude", exact_scene_count="437",
                    include_tag_logic="ALL", include_tag_ids_all="7992",
                    include_tags_all="ACT: Grinding",
                    exclude_tag_logic="ANY", exclude_tag_ids_any="8083",
                    exclude_tags_any="PROD: Gonzo"),
            csv_row(channel_number="860", channel_name="Double Feature",
                    channel_family="performer_pair", exact_scene_count="24",
                    include_performer_logic="ALL", include_performer_ids_all="4041|5373",
                    include_performers_all="Dylan Ryan|Krysta Kaos"),
            csv_row(channel_number="800", channel_name="Bukkake Nights",
                    channel_family="studio_tag", exact_scene_count="174",
                    include_tag_logic="ALL", include_tag_ids_all="8668",
                    include_tags_all="ACT: Bukkake",
                    include_studio_logic="ANY", include_studio_ids_any="2539",
                    include_studios_any="Premium Bukkake"),
        ]
        path = write_csv(tmp_path / "in.csv", rows)

        first = importer.import_csv(path)
        second = importer.import_csv(path)
        assert first == second  # ids, seeds, ordering all stable

        by_number = {c["number"]: c for c in first["channels"]}
        assert by_number[100]["section"] == "performers"
        assert by_number[100]["source"] == {"type": "filter", "performers": ["157"]}
        assert by_number[700]["section"] == "general"
        assert by_number[700]["source"] == {
            "type": "filter", "tags": ["7992"], "excludeTags": ["8083"],
        }
        assert by_number[860]["source"] == {
            "type": "filter", "performers": ["4041", "5373"],
        }
        assert by_number[800]["source"] == {
            "type": "filter", "tags": ["8668"], "studios": ["2539"],
        }
        assert by_number[700]["sourceLabel"] == "ACT: Grinding (without PROD: Gonzo)"
        assert by_number[860]["sourceLabel"] == "Dylan Ryan, Krysta Kaos"
        assert by_number[800]["sourceLabel"] == "Premium Bukkake · ACT: Bukkake"
        for channel in first["channels"]:
            assert channel["glyph"] in contract.GLYPHS

    def test_import_rejects_bad_rows(self, tmp_path):
        importer = _importer()
        cases = [
            csv_row(channel_number="42", channel_name="Too Low",
                    channel_family="tag_spotlight", exact_scene_count="5",
                    include_tag_ids_all="1"),
            csv_row(channel_number="100", channel_name="", channel_family="tag_spotlight",
                    exact_scene_count="5", include_tag_ids_all="1"),
            csv_row(channel_number="100", channel_name="x" * 81,
                    channel_family="tag_spotlight", exact_scene_count="5",
                    include_tag_ids_all="1"),
            csv_row(channel_number="100", channel_name="Mystery", channel_family="mystery",
                    exact_scene_count="5", include_tag_ids_all="1"),
            csv_row(channel_number="100", channel_name="No Include",
                    channel_family="tag_include_exclude", exact_scene_count="5",
                    exclude_tag_ids_any="2"),
            csv_row(channel_number="100", channel_name="Bad Logic",
                    channel_family="tag_pair", exact_scene_count="5",
                    include_tag_logic="ANY", include_tag_ids_all="1|2"),
            csv_row(channel_number="100", channel_name="Multi Studio",
                    channel_family="studio_tag", exact_scene_count="5",
                    include_studio_ids_any="1|2"),
        ]
        for case in cases:
            path = write_csv(tmp_path / "in.csv", [case])
            with pytest.raises(SystemExit):
                importer.import_csv(path)

    def test_import_rejects_duplicate_numbers(self, tmp_path):
        importer = _importer()
        base = csv_row(channel_number="100", channel_name="A", channel_family="tag_spotlight",
                       exact_scene_count="5", include_tag_ids_all="1")
        twin = csv_row(channel_number="100", channel_name="B", channel_family="tag_spotlight",
                       exact_scene_count="5", include_tag_ids_all="2")
        path = write_csv(tmp_path / "in.csv", [base, twin])
        with pytest.raises(SystemExit):
            importer.import_csv(path)


# ---------------------------------------------------------------------------
# Stable-key identity (v0.6)
# ---------------------------------------------------------------------------

MADONNA_BASE = {"channel_number": "407", "channel_name": "Madonna: Script Pending",
                "channel_family": "studio_spotlight", "exact_scene_count": "169",
                "include_studio_logic": "ANY", "include_studio_ids_any": "819",
                "include_studios_any": "Madonna"}


def _legacy_identity(number: str, name: str) -> tuple[str, int]:
    legacy_id = "net_" + hashlib.sha1(f"{number}|{name}".encode()).hexdigest()[:8]
    legacy_seed = int(
        hashlib.sha1(f"seed|{number}|{name}".encode()).hexdigest()[:8], 16,
    ) % 2_147_483_648
    return legacy_id, legacy_seed


class TestStableKey:
    def test_legacy_row_reproduces_historical_identity(self, tmp_path):
        importer = _importer()
        doc = importer.import_csv(write_csv(tmp_path / "in.csv", [csv_row(**MADONNA_BASE)]))
        channel = doc["channels"][0]
        legacy_id, legacy_seed = _legacy_identity("407", "Madonna: Script Pending")
        assert channel["id"] == legacy_id
        assert channel["seed"] == legacy_seed

    def test_survivor_stable_key_reproduces_identity(self, tmp_path):
        importer = _importer()
        row = csv_row(**MADONNA_BASE, stable_key="407|Madonna: Script Pending")
        doc = importer.import_csv(write_csv(tmp_path / "in.csv", [row]))
        channel = doc["channels"][0]
        legacy_id, legacy_seed = _legacy_identity("407", "Madonna: Script Pending")
        assert channel["id"] == legacy_id
        assert channel["seed"] == legacy_seed

    def test_display_name_change_with_fixed_key_keeps_identity(self, tmp_path):
        importer = _importer()
        row = csv_row(**{**MADONNA_BASE, "channel_name": "Madonna Network"},
                      stable_key="407|Madonna: Script Pending")
        doc = importer.import_csv(write_csv(tmp_path / "in.csv", [row]))
        channel = doc["channels"][0]
        legacy_id, legacy_seed = _legacy_identity("407", "Madonna: Script Pending")
        assert channel["name"] == "Madonna Network"
        assert channel["id"] == legacy_id
        assert channel["seed"] == legacy_seed

    def test_channel_number_change_with_fixed_key_keeps_identity(self, tmp_path):
        importer = _importer()
        row = csv_row(**{**MADONNA_BASE, "channel_number": "512"},
                      stable_key="407|Madonna: Script Pending")
        doc = importer.import_csv(write_csv(tmp_path / "in.csv", [row]))
        channel = doc["channels"][0]
        legacy_id, legacy_seed = _legacy_identity("407", "Madonna: Script Pending")
        assert channel["number"] == 512
        assert channel["id"] == legacy_id
        assert channel["seed"] == legacy_seed

    def test_reimport_is_byte_deterministic(self, tmp_path):
        importer = _importer()
        rows = [
            csv_row(**MADONNA_BASE, stable_key="407|Madonna: Script Pending"),
            csv_row(channel_number="512", channel_name="Dress + Bedroom",
                    channel_family="tag_pair", exact_scene_count="214",
                    include_tag_logic="ALL", include_tag_ids_all="7978|8027",
                    stable_key="jw:v1:tagpair:7978:8027"),
        ]
        path = write_csv(tmp_path / "in.csv", rows)
        first = importer.import_csv(path)
        second = importer.import_csv(path)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_duplicate_stable_keys_rejected(self, tmp_path):
        importer = _importer()
        rows = [
            csv_row(channel_number="100", channel_name="A", channel_family="tag_spotlight",
                    exact_scene_count="5", include_tag_ids_all="1",
                    stable_key="jw:v1:tag:1"),
            csv_row(channel_number="101", channel_name="B", channel_family="tag_spotlight",
                    exact_scene_count="5", include_tag_ids_all="2",
                    stable_key="jw:v1:tag:1"),
        ]
        with pytest.raises(SystemExit):
            importer.import_csv(write_csv(tmp_path / "in.csv", rows))

    def test_derived_id_collision_rejected(self, tmp_path):
        # Two rows whose (number, name) hash to the same id: an 8-hex digest
        # collision is contrived, so simulate via stable_key == legacy basis.
        importer = _importer()
        rows = [
            csv_row(channel_number="100", channel_name="Twin", channel_family="tag_spotlight",
                    exact_scene_count="5", include_tag_ids_all="1"),
            csv_row(channel_number="101", channel_name="x", channel_family="tag_spotlight",
                    exact_scene_count="5", include_tag_ids_all="2",
                    stable_key="100|Twin"),
        ]
        with pytest.raises(SystemExit):
            importer.import_csv(write_csv(tmp_path / "in.csv", rows))


# ---------------------------------------------------------------------------
# ANY semantics + metadata criteria (v0.6)
# ---------------------------------------------------------------------------

class TestAnySemantics:
    def test_performer_any_projects_performers_any(self):
        importer = _importer()
        row = csv_row(channel_number="300", channel_name="Rare Performers",
                      channel_family="special_rare_performers", exact_scene_count="5212",
                      include_performer_logic="ANY",
                      include_performer_ids_all="11|874|1063",
                      include_performers_all="Ryan Madison|Kelly Madison|Martin Stein")
        # build_source keeps the importer's lexical id order; the projection
        # canonicalizes numerically — union semantics either way.
        assert importer.build_source(row) == {
            "type": "filter", "performersAny": ["1063", "11", "874"],
        }
        assert lineup.build_scene_filter(importer.build_source(row)) == {
            "performers": {"value": ["11", "874", "1063"], "modifier": "INCLUDES"},
        }

    def test_single_studio_any_keeps_legacy_shape(self):
        importer = _importer()
        row = csv_row(channel_number="407", channel_name="Madonna",
                      channel_family="studio_spotlight", exact_scene_count="169",
                      include_studio_logic="ANY", include_studio_ids_any="819")
        assert importer.build_source(row) == {"type": "filter", "studios": ["819"]}

    def test_multi_studio_any_projects_union(self):
        importer = _importer()
        row = csv_row(channel_number="320", channel_name="Fringe Studios",
                      channel_family="special_fringe_studios", exact_scene_count="2035",
                      include_studio_logic="ANY", include_studio_ids_any="60|9|512")
        source = importer.build_source(row)
        assert source == {"type": "filter", "studiosAny": ["512", "60", "9"]}
        assert lineup.build_scene_filter(source) == {
            "studios": {"value": ["9", "60", "512"], "modifier": "INCLUDES", "depth": -1},
        }

    def test_multi_studio_all_projects_intersection(self):
        importer = _importer()
        row = csv_row(channel_number="321", channel_name="Two Networks",
                      channel_family="studio_tag", exact_scene_count="40",
                      include_studio_logic="ALL", include_studio_ids_any="60|9",
                      include_tag_ids_all="8020")
        source = importer.build_source(row)
        assert source == {"type": "filter", "studios": ["60", "9"], "tags": ["8020"]}
        assert lineup.build_scene_filter(source)["studios"] == {
            "value": ["9", "60"], "modifier": "INCLUDES_ALL", "depth": -1,
        }

    def test_multi_studio_blank_logic_rejected(self, tmp_path):
        importer = _importer()
        row = csv_row(channel_number="100", channel_name="Ambiguous",
                      channel_family="studio_tag", exact_scene_count="5",
                      include_studio_ids_any="1|2", include_tag_ids_all="3")
        with pytest.raises(SystemExit):
            importer.import_csv(write_csv(tmp_path / "in.csv", [row]))

    def test_bad_performer_logic_rejected(self, tmp_path):
        importer = _importer()
        row = csv_row(channel_number="100", channel_name="Bad", channel_family="tag_spotlight",
                      exact_scene_count="5", include_tag_ids_all="1",
                      include_performer_logic="MAYBE")
        with pytest.raises(SystemExit):
            importer.import_csv(write_csv(tmp_path / "in.csv", [row]))

    def test_any_label_summarizes_large_lists(self):
        importer = _importer()
        row = csv_row(channel_number="300", channel_name="Rare Performers",
                      channel_family="special_rare_performers", exact_scene_count="5212",
                      include_performer_logic="ANY",
                      include_performer_ids_all="1|2|3|4|5",
                      include_performers_all="A|B|C|D|E")
        assert importer.source_label(row) == "5 performers (any)"


class TestMetadataImport:
    def base_row(self, **overrides):
        return csv_row(channel_number="350", channel_name="2010s Vault",
                       channel_family="special_era", exact_scene_count="10789",
                       **overrides)

    def test_metadata_columns_compile_to_source_criteria(self):
        importer = _importer()
        row = self.base_row(scene_date_from="2010-01-01", scene_date_to="2019-12-31")
        assert importer.build_source(row) == {
            "type": "filter", "date": {"from": "2010-01-01", "to": "2019-12-31"},
        }

    def test_metadata_only_row_imports_and_labels(self, tmp_path):
        importer = _importer()
        rows = [
            self.base_row(scene_date_from="2010-01-01", scene_date_to="2019-12-31"),
            csv_row(channel_number="351", channel_name="Feature Length",
                    channel_family="special_duration", exact_scene_count="1566",
                    duration_min_seconds="3600"),
            csv_row(channel_number="352", channel_name="New Arrivals",
                    channel_family="special_recent", exact_scene_count="4620",
                    created_within_days="180"),
        ]
        doc = importer.import_csv(write_csv(tmp_path / "in.csv", rows))
        by_name = {c["name"]: c for c in doc["channels"]}
        assert by_name["2010s Vault"]["source"] == {
            "type": "filter", "date": {"from": "2010-01-01", "to": "2019-12-31"},
        }
        assert by_name["2010s Vault"]["sourceLabel"] == "2010–2019"
        assert by_name["Feature Length"]["source"] == {
            "type": "filter", "duration": {"min": 3600},
        }
        assert by_name["Feature Length"]["sourceLabel"] == "60+ min"
        assert by_name["New Arrivals"]["source"] == {
            "type": "filter", "createdAt": {"withinDays": 180},
        }
        assert by_name["New Arrivals"]["sourceLabel"] == "last 180 days"

    def test_jav_exclusion_rides_metadata_sources(self):
        importer = _importer()
        row = self.base_row(scene_date_from="2010-01-01", scene_date_to="2019-12-31",
                            exclude_tag_logic="ANY", exclude_tag_ids_any="9320",
                            exclude_tags_any="JAV")
        source = importer.build_source(row)
        assert source["excludeTags"] == ["9320"]
        assert lineup.build_scene_filter(source)["tags"] == {
            "value": [], "modifier": "INCLUDES_ALL", "depth": -1, "excludes": ["9320"],
        }

    @pytest.mark.parametrize("overrides", [
        {"scene_date_from": "2010-01-01"},                       # missing upper bound
        {"scene_date_from": "2010-01-01", "scene_date_to": "x"}, # not a date
        {"duration_min_seconds": "hour"},                        # not a number
        {"created_within_days": "0"},                            # not positive
    ])
    def test_malformed_metadata_rejected(self, tmp_path, overrides):
        importer = _importer()
        with pytest.raises(SystemExit):
            importer.import_csv(write_csv(
                tmp_path / "in.csv", [self.base_row(**overrides)]))


class TestMetadataProjection:
    def test_date_between(self):
        source = {"type": "filter", "date": {"from": "2010-01-01", "to": "2019-12-31"}}
        assert lineup.build_scene_filter(source) == {
            "date": {"value": "2010-01-01", "value2": "2019-12-31",
                     "modifier": "BETWEEN"},
        }

    def test_duration_min_is_inclusive_between(self):
        source = {"type": "filter", "duration": {"min": 3600}}
        assert lineup.build_scene_filter(source) == {
            "duration": {"value": 3600, "value2": 2147483647, "modifier": "BETWEEN"},
        }

    def test_created_at_resolves_cutoff_at_query_time(self):
        source = {"type": "filter", "createdAt": {"withinDays": 180}}
        value = lineup.build_scene_filter(source)["created_at"]
        assert value["modifier"] == "GREATER_THAN"
        assert value["value"] == lineup.created_cutoff(180)  # today, at call time
        assert len(value["value"]) == 10  # bare YYYY-MM-DD (midnight-inclusive day)

    def test_created_cutoff_matches_extraction_anchor(self):
        # The 2026-09-08 extraction used created_at[:10] >= 2026-03-12 for its
        # 180-day window: 180 calendar days back from 09-08 is 03-12 exactly.
        assert lineup.created_cutoff(180, _dt.date(2026, 9, 8)) == "2026-03-12"
        assert lineup.created_cutoff(180, _dt.date(2026, 9, 9)) == "2026-03-13"

    def test_effective_epoch_empty_for_static_sources(self):
        assert lineup.effective_epoch({"type": "filter", "tags": ["1"]}) == ""
        assert lineup.effective_epoch({"type": "filter", "duration": {"min": 60}}) == ""

    def test_rotation_version_tracks_cutoff_epoch(self):
        source = {"type": "filter", "createdAt": {"withinDays": 180}}
        day_a = lineup.rotation_version(source, "shuffle", 7, 50, today=_dt.date(2026, 9, 8))
        day_b = lineup.rotation_version(source, "shuffle", 7, 50, today=_dt.date(2026, 9, 9))
        assert day_a != day_b
        expected = hashlib.sha1(json.dumps(
            [*lineup.source_key(source), "shuffle", 7, 50, "epoch:2026-03-12"],
            sort_keys=True,
        ).encode()).hexdigest()[:12]
        assert day_a == expected

    def test_rotation_version_legacy_formula_unchanged(self):
        # A static source must hash the exact historical basis: an importer
        # upgrade never churns existing rotations.
        source = {"type": "filter", "performers": ["157"], "excludeTags": ["9320"]}
        expected = hashlib.sha1(json.dumps(
            [*lineup.source_key(source), "shuffle", 7, 50], sort_keys=True,
        ).encode()).hexdigest()[:12]
        assert lineup.rotation_version(source, "shuffle", 7, 50) == expected

    def test_source_key_distinguishes_any_from_all(self):
        all_src = {"type": "filter", "performers": ["1", "2"]}
        any_src = {"type": "filter", "performersAny": ["1", "2"]}
        studios_all = {"type": "filter", "studios": ["1", "2"]}
        studios_any = {"type": "filter", "studiosAny": ["1", "2"]}
        assert lineup.source_key(all_src) != lineup.source_key(any_src)
        assert lineup.source_key(studios_all) != lineup.source_key(studios_any)
        assert lineup.source_key(any_src) != lineup.source_key(studios_any)
        date_src = {"type": "filter", "date": {"from": "2020-01-01", "to": "2026-12-31"}}
        date_alt = {"type": "filter", "date": {"from": "2020-01-01", "to": "2027-12-31"}}
        assert lineup.source_key(date_src) != lineup.source_key(date_alt)


class TestLoaderV6:
    def test_new_source_kinds_load(self, net_path):
        net_path.write_text(json.dumps({"channels": [
            net_channel(id="net_a0000001", number=300, section="general",
                        family="special_rare_performers", glyph="\uf005",
                        source={"type": "filter", "performersAny": ["11", "874"],
                                "excludeTags": ["9320"]}),
            net_channel(id="net_a0000002", number=320, section="general",
                        family="special_fringe_studios", glyph="\uf005",
                        source={"type": "filter", "studiosAny": ["60", "9"]}),
            net_channel(id="net_a0000003", number=350, section="general",
                        family="special_era", glyph="\uf005",
                        source={"type": "filter",
                                "date": {"from": "2010-01-01", "to": "2019-12-31"}}),
            net_channel(id="net_a0000004", number=351, section="general",
                        family="special_duration", glyph="\uf005",
                        source={"type": "filter", "duration": {"min": 3600}}),
            net_channel(id="net_a0000005", number=352, section="general",
                        family="special_recent", glyph="\uf005",
                        source={"type": "filter", "createdAt": {"withinDays": 180}}),
        ]}), encoding="utf-8")
        document = networks.load()
        assert len(document["channels"]) == 5

    @pytest.mark.parametrize("source", [
        {"type": "filter", "date": {"from": "2020-01-01"}},          # missing bound
        {"type": "filter", "date": {"from": "2020-1-01", "to": "x"}},
        {"type": "filter", "duration": {"min": -1}},
        {"type": "filter", "duration": {"min": "3600"}},             # string, not int
        {"type": "filter", "createdAt": {"withinDays": 0}},
        {"type": "filter", "performersAny": ["abc"]},                # no real include
    ])
    def test_malformed_new_criteria_raise(self, net_path, source):
        networks_file(net_path.parent, [net_channel(source=source)])
        with pytest.raises(NetworksError):
            networks.load()

    def test_load_takes_an_explicit_path(self, net_path, tmp_path):
        net_path.write_text(json.dumps({"channels": [net_channel()]}), encoding="utf-8")
        other = tmp_path / "preview.json"
        other.write_text(json.dumps({"channels": [
            net_channel(id="net_b0000001", number=777),
        ]}), encoding="utf-8")
        document = networks.load(other)
        assert [c["number"] for c in document["channels"]] == [777]
        assert networks.load()["channels"][0]["number"] == 100  # PATH untouched


class TestGoldenRegression:
    def test_production_csv_compiles_to_the_live_networks_json(self):
        """The upgraded importer must leave the current production catalog
        byte-for-byte identical (no new columns are populated on it)."""
        importer = _importer()
        doc = importer.import_csv(importer.DEFAULT_CSV)
        doc["revision"] = importer.revision(doc)
        live = json.loads(
            (REPO / "justwatch" / "networks.json").read_text(encoding="utf-8"))
        assert doc == live


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestLoader:
    def test_missing_file_is_an_empty_tier(self, net_path):
        assert networks.load() == {"revision": "", "channels": []}
        assert networks.directory_payload() is None

    def test_loads_and_groups_sections(self, net_path):
        net_path.write_text(json.dumps({"channels": [
            net_channel(),
            net_channel(id="net_21e28b41", number=860, family="performer_pair"),
            net_channel(id="net_f4c64807", number=700, section="general",
                        family="tag_include_exclude", glyph="\uf005",
                        source={"type": "filter", "tags": ["7992"], "excludeTags": ["8083"]}),
            net_channel(id="net_aaa00001", number=376, section="studios",
                        family="studio_spotlight", glyph="\uf3a5",
                        source={"type": "filter", "studios": ["60"]}),
        ]}), encoding="utf-8")
        document = networks.load()
        assert len(document["channels"]) == 4
        assert document["revision"]
        grouped = networks.sections()
        assert [c["number"] for c in grouped["performers"]] == [100, 860]
        assert [c["number"] for c in grouped["general"]] == [700]
        assert [c["number"] for c in grouped["studios"]] == [376]
        assert networks.get("net_738dc8a1")["name"] == "Mick Blue After Dark"
        assert networks.get("ch_deadbeef") is None

    @pytest.mark.parametrize("mutation", [
        {"number": 42},
        {"glyph": "\uf02c"},
        {"section": "mixes"},
        {"sort": "popular"},
        {"source": {"type": "tag", "id": "5"}},
        {"source": {"type": "filter", "excludeTags": ["9"]}},
        {"seed": None},
        {"seed": -1},
        {"id": "ch_deadbeef"},
    ])
    def test_malformed_channels_raise(self, net_path, mutation):
        channel = net_channel(**mutation)
        networks_file(net_path.parent, [channel])
        with pytest.raises(NetworksError):
            networks.load()

    def test_duplicate_numbers_raise(self, net_path):
        networks_file(net_path.parent, [net_channel(), net_channel(id="net_21e28b41")])
        with pytest.raises(NetworksError):
            networks.load()


# ---------------------------------------------------------------------------
# Filter-source projection
# ---------------------------------------------------------------------------


class TestFilterProjection:
    def test_tags_include_all_with_excludes(self):
        source = {"type": "filter", "tags": ["8077", "8079"], "excludeTags": ["8083"]}
        assert lineup.build_scene_filter(source) == {
            "tags": {"value": ["8077", "8079"], "modifier": "INCLUDES_ALL",
                     "depth": -1, "excludes": ["8083"]},
        }

    def test_performers_intersect_and_studios_are_hierarchical(self):
        source = {"type": "filter", "performers": ["5373", "4041"], "studios": ["2539"]}
        assert lineup.build_scene_filter(source) == {
            "performers": {"value": ["4041", "5373"], "modifier": "INCLUDES_ALL"},
            "studios": {"value": ["2539"], "modifier": "INCLUDES_ALL", "depth": -1},
        }

    def test_ordering_and_dupes_do_not_change_identity(self):
        a = {"type": "filter", "tags": ["2", "1"], "performers": ["9"]}
        b = {"type": "filter", "performers": ["9", "9"], "tags": ["1", "2"]}
        assert lineup.source_key(a) == lineup.source_key(b)
        assert (lineup.rotation_version(a, "shuffle", 7, 50)
                == lineup.rotation_version(b, "shuffle", 7, 50))

    def test_filter_without_includes_is_rejected(self):
        with pytest.raises(ValueError):
            lineup.build_scene_filter({"type": "filter", "excludeTags": ["8083"]})

    def test_rotation_version_tracks_filter_content(self):
        a = {"type": "filter", "tags": ["1"]}
        b = {"type": "filter", "tags": ["2"]}
        assert lineup.rotation_version(a, "shuffle", 7, 50) != \
            lineup.rotation_version(b, "shuffle", 7, 50)


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------


class TestDirectoryNetworks:
    def test_directory_carries_networks_block(self, envelope, net_path, fake_client):
        net_path.write_text(json.dumps({"channels": [net_channel()]}), encoding="utf-8")
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        assert result["networks"]["revision"] == networks.revision([net_channel()])
        channel = result["networks"]["channels"][0]
        assert channel["number"] == 100
        assert channel["id"] == "net_738dc8a1"
        assert channel["programmingMode"] == "fixed"

    def test_directory_omits_networks_without_file(self, envelope, net_path, fake_client):
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        assert "networks" not in result

    def test_capabilities_advertise_networks(self, envelope, fake_client):
        caps = dispatch(envelope, {"mode": "Capabilities"}, fake_client)
        assert caps["features"]["networks"] == {"version": 1, "minNumber": 100}


class TestNetworkLineup:
    def test_lineup_serves_network_channels(self, envelope, net_path, fake_client):
        net_path.write_text(json.dumps({"channels": [net_channel()]}), encoding="utf-8")
        result = dispatch(
            envelope, {"mode": "Lineup", "channelId": "net_738dc8a1"}, fake_client,
        )
        assert result["channelId"] == "net_738dc8a1"
        assert result["revision"] == networks.revision([net_channel()])
        assert result["rotationVersion"].startswith("n")
        assert result["sourceTotal"] == 2
        variables = fake_client.last_variables()
        assert variables["scene_filter"] == {
            "performers": {"value": ["157"], "modifier": "INCLUDES_ALL"},
        }
        assert variables["filter"]["sort"].startswith("random_")

    def test_lineup_unknown_network_falls_through_to_catalog(
        self, envelope, net_path, fake_client,
    ):
        net_path.write_text(json.dumps({"channels": [net_channel()]}), encoding="utf-8")
        with pytest.raises(LookupError):
            dispatch(envelope, {"mode": "Lineup", "channelId": "net_00000000"}, fake_client)


class TestFullDirectory:
    def test_sections_come_from_networks_without_stash_queries(
        self, envelope, net_path, fake_client,
    ):
        net_path.write_text(json.dumps({"channels": [
            net_channel(),
            net_channel(id="net_f4c64807", number=700, section="general",
                        family="tag_include_exclude", glyph="\uf005",
                        source={"type": "filter", "tags": ["7992"], "excludeTags": ["8083"]}),
        ]}), encoding="utf-8")
        result = dispatch(envelope, {"mode": "FullDirectory"}, fake_client)
        assert fake_client.calls == []  # pure file reads now
        assert result["general"]["tagChannels"][0]["origin"] == "network"
        assert result["performers"]["channels"][0]["id"] == "net_738dc8a1"
        assert result["studios"]["channels"] == []
        assert "networksNote" in result
