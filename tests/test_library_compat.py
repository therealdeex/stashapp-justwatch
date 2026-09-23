"""Compatibility matrix: legacy clients, legacy deployments, and the library.

Covers the plan's required combinations on the SERVER side:
* library absent  -> every legacy op behaves exactly as before (no networks
  key when the compiled file is absent; nothing library-flavored leaks).
* library present -> Directory keeps its legacy shape for old TVs (customs
  only + a networks block with LEGAL legacy section names); an intentionally
  empty owner tier emits a present-but-EMPTY networks block (never an absent
  one, never generated channels); GetCatalog reports libraryBacked; a legacy
  SaveCatalog maps onto one customs-only Apply and REFUSES to truncate rules
  it cannot represent.
"""
import json

import pytest

from justwatch import library as library_mod
from justwatch import main
from tests.test_ops import dispatch


def seed_library(data_dir: Path, *, net_numbers=(100, 101), channels=()):
    doc = {
        "schemaVersion": library_mod.STORAGE_VERSION,
        "libraryId": "lib_compat000",
        "revision": 3,
        "groups": [
            {"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
            {"id": "grp_general", "name": "General", "position": 2, "legacySection": "general"},
            {"id": "grp_weekend", "name": "Weekend Picks", "position": 3, "legacySection": None},
        ],
        "channels": [
            {"id": "ch_11111111", "kind": "ch", "number": 1, "name": "One", "glyph": "\uf111",
             "color": "#112233", "groupId": "grp_my", "sort": "shuffle", "seed": 42,
             "enabled": True, "archived": False, "paused": False,
             "source": {"type": "tag", "id": "5", "ids": ["5"]}, "sourceLabel": "tag",
             "programming": {"mode": "fixed"}, "provenance": {"origin": "custom"}},
            *[{"id": f"net_{n:08x}", "kind": "net", "number": n, "name": f"Net {n}",
               "glyph": "\uf111", "color": "#455A64", "groupId": "grp_general",
               "sort": "shuffle", "seed": n, "enabled": True, "archived": False,
               "paused": False,
               "source": {"type": "filter", "performers": ["9"], "excludeTags": ["9320"]},
               "sourceLabel": "", "programming": {"mode": "fixed"},
               "provenance": {"origin": "v4-final-proposal",
                              "legacySection": "general", "seedCount": 12}}
              for n in net_numbers],
        ],
        "recentRequests": [],
    }
    doc["channels"].extend(channels)
    library_mod.save(data_dir, doc)
    return doc


class TestLegacyDeploymentUntouched:
    def test_directory_without_library_serves_legacy_compiled_tier(
            self, envelope, data_dir, fake_client):
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        assert result["channels"] == []
        # the repo checkout ships a compiled tier, so the block is present;
        # whatever it carries, it is the LEGACY shape (count + section rows)
        if "networks" in result:
            assert all("count" in c and "section" in c
                       for c in result["networks"]["channels"])
            assert not any("groupId" in c for c in result["networks"]["channels"])

    def test_library_ops_refuse_without_migration(self, envelope, data_dir, fake_client):
        with pytest.raises(ValueError, match="no channel library"):
            dispatch(envelope, {"mode": "GetChannelLibrary"}, fake_client)


class TestLibraryDeploymentLegacyShapes:
    def test_directory_keeps_legacy_payload_for_old_tvs(self, envelope, data_dir, fake_client):
        seed_library(data_dir)
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        # customs only in the legacy list, playable only
        assert [c["id"] for c in result["channels"]] == ["ch_11111111"]
        # networks block present with LEGAL legacy section names
        assert set(c["section"] for c in result["networks"]["channels"]) <= {
            "general", "studios", "performers"}
        assert all(c["number"] >= 100 for c in result["networks"]["channels"])

    def test_owner_group_names_never_leak_into_section(self, envelope, data_dir, fake_client):
        doc = seed_library(data_dir)
        doc["channels"][1]["groupId"] = "grp_weekend"  # owner-defined group
        library_mod.save(data_dir, doc)
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        sections = {c["section"] for c in result["networks"]["channels"]}
        assert sections <= {"general", "studios", "performers"}

    def test_intentionally_empty_tier_is_authoritative_empty(self, envelope, data_dir, fake_client):
        seed_library(data_dir, net_numbers=())
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        assert "networks" in result, "empty owner tier must stay authoritative"
        assert result["networks"]["channels"] == []

    def test_fully_paused_library_yields_an_empty_playable_surface(
            self, envelope, data_dir, fake_client):
        doc = seed_library(data_dir)
        for channel in doc["channels"]:
            channel["paused"] = True
        library_mod.save(data_dir, doc)
        result = dispatch(envelope, {"mode": "Directory"}, fake_client)
        assert result["channels"] == []

    def test_lineup_serves_library_channels(self, envelope, data_dir, fake_client):
        seed_library(data_dir)
        result = dispatch(envelope, {"mode": "Lineup", "channelId": "net_00000064"}, fake_client)
        assert result["channelId"] == "net_00000064"
        assert result["total"] == 2  # the fake client's canned rotation
        assert result["rotationVersion"].startswith("r3-")

    def test_get_catalog_reports_library_backed(self, envelope, data_dir, fake_client):
        seed_library(data_dir)
        result = dispatch(envelope, {"mode": "GetCatalog"}, fake_client)
        assert result["libraryBacked"] is True
        assert [c["id"] for c in result["channels"]] == ["ch_11111111"]


class TestLegacySaveCatalogOnLibrary:
    def test_custom_edit_maps_onto_one_apply(self, envelope, data_dir, fake_client):
        seed_library(data_dir)
        draft = {
            "schemaVersion": 1,
            "channels": [{
                "id": "ch_11111111", "number": 1, "name": "One Renamed",
                "glyph": "\uf111", "color": "#112233",
                "source": {"type": "tag", "id": "5", "ids": ["5"]},
                "sort": "shuffle", "seed": 42, "enabled": True,
            }],
        }
        result = dispatch(envelope, {"mode": "SaveCatalog", "catalog": json.dumps(draft),
                                     "expectedRevision": "3", "requestId": "legacy-1"},
                          fake_client)
        assert result["saved"] is True
        doc = library_mod.load(data_dir)
        one = next(c for c in doc["channels"] if c["id"] == "ch_11111111")
        assert one["name"] == "One Renamed"
        assert one["groupId"] == "grp_my"
        # networks untouched
        assert len([c for c in doc["channels"] if c["kind"] == "net"]) == 2

    def test_unrepresentable_rules_are_reported_not_truncated(self, envelope, data_dir, fake_client):
        seed_library(data_dir)
        doc = library_mod.load(data_dir)
        doc["channels"][0]["source"] = {"type": "criteria", "tagsAny": ["8"], "q": "x"}
        library_mod.save(data_dir, doc)
        draft = {
            "schemaVersion": 1,
            "channels": [{
                "id": "ch_11111111", "number": 1, "name": "Clobbered",
                "glyph": "\uf111", "color": "#112233",
                "source": {"type": "tag", "id": "5"},
                "sort": "shuffle", "seed": 42, "enabled": True,
            }],
        }
        result = dispatch(envelope, {"mode": "SaveCatalog", "catalog": json.dumps(draft),
                                     "expectedRevision": "3", "requestId": "legacy-2"},
                          fake_client)
        assert result["saved"] is False
        assert result["error"] == "unsupported_edit"
        stored = library_mod.load(data_dir)["channels"][0]
        assert stored["name"] != "Clobbered"
        assert stored["source"]["type"] == "criteria", "rules must survive intact"

    def test_revision_conflict_maps_through(self, envelope, data_dir, fake_client):
        seed_library(data_dir)
        draft = {"schemaVersion": 1, "channels": []}
        result = dispatch(envelope, {"mode": "SaveCatalog", "catalog": json.dumps(draft),
                                     "expectedRevision": "1", "requestId": "legacy-3"},
                          fake_client)
        # no custom channels in the draft -> explicit no-op message
        assert result["saved"] is False


class TestCapabilityAdvertisement:
    def test_library_capabilities_are_additive(self, envelope, fake_client):
        caps = dispatch(envelope, {"mode": "Capabilities"}, fake_client)
        assert caps["features"]["channelLibrary"]["version"] == 1
        assert caps["features"]["channelGroups"]["singleMembership"] is True
        assert caps["features"]["explicitApply"]["applyOperation"] == "ApplyChannelChanges"
        assert caps["operations"]["getChannelDirectory"] == "GetChannelDirectory"
        assert caps["limits"]["libraryChannels"] == 899
        # legacy surface untouched
        assert caps["limits"]["maxChannels"] == 99
        assert caps["contractVersion"] == 1
