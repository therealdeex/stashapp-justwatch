"""The network tier: importer, strict loader, filter projection, op wiring."""

from __future__ import annotations

import csv
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
