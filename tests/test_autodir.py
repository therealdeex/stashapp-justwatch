"""Parity tests for the auto-channel tiering port (autodir.py).

These mirror the TV app's behavior (JustWatchViewModel tiering + JustWatchDial
alias matching). The curated specs come from the app's JustWatchChannelSpecs.kt
via tools/extract_specs.py — the tests assert against the same vendored data.
"""

from __future__ import annotations

import json
from pathlib import Path

from justwatch import autodir

SPECS = json.loads(
    (Path(__file__).resolve().parent.parent / "justwatch" / "specs.json").read_text(
        encoding="utf-8",
    ),
)


class TestAliasMatching:
    def test_exact_beats_prefix_beats_substring(self):
        assert autodir.alias_score("kitchen", "kitchen") == 3
        assert autodir.alias_score("kitchen", "kitchen sex") == 2
        # "SET: Kitchen" normalizes to "setkitchen": only a substring of
        # "kitchen" -- which is why specs carry the bare alias too, and
        # bestAliasScore takes the max across them (same as the TV app).
        assert autodir.alias_score("SET: Kitchen", "kitchen") == 1
        assert autodir.best_alias_score(["SET: Kitchen", "kitchen"], "kitchen") == 3

    def test_short_substrings_do_not_match(self):
        # "oil" is 3 chars — must NOT match "soil"
        assert autodir.alias_score("oil", "soil") == 0

    def test_long_substrings_match(self):
        assert autodir.alias_score("massage", "oil massage scene") == 1

    def test_punctuation_is_stripped(self):
        assert autodir.alias_score("SET: Pool/water", "SET: Pool/water") == 3
        assert autodir.alias_score("act: anal sex", "ACT: Anal Sex") == 3

    def test_match_tags_orders_by_score_then_count(self):
        tags = [
            {"name": "kitchen floor", "sceneCount": 50},
            {"name": "kitchen", "sceneCount": 10},
            {"name": "unrelated", "sceneCount": 99},
        ]
        matched = autodir.match_tags(["kitchen", "food"], tags)
        # exact "kitchen" (score 3) beats the prefix match despite fewer scenes
        assert [t["name"] for t in matched] == ["kitchen", "kitchen floor"]


class TestTagGroupChannels:
    def test_known_tag_channel_resolves_and_numbers_at_201(self):
        all_tags = [
            {"id": "1", "name": "SET: Bedroom", "sceneCount": 4},
            {"id": "2", "name": "bedroom", "sceneCount": 2},
        ]
        rows = autodir.tag_group_channels(all_tags)
        boudoir = rows[0]
        assert boudoir["name"] == "The Boudoir Channel"
        assert boudoir["number"] == 201
        assert boudoir["count"] == 6
        assert not boudoir["offAir"]

    def test_unmatched_or_unused_tags_are_off_air(self):
        rows = autodir.tag_group_channels([{"id": "1", "name": "comedy", "sceneCount": 5}])
        assert all(r["offAir"] for r in rows if r["name"] == "The Boudoir Channel")


class TestPerformerTiers:
    def performers(self):
        return [
            {"id": "1", "name": "Star", "sceneCount": 30, "tags": []},
            {"id": "2", "name": "Mid Inked", "sceneCount": 7, "tags": ["BODY: Tattooed"]},
            {"id": "3", "name": "Mid Plain", "sceneCount": 6, "tags": []},
            {"id": "4", "name": "Small Own", "sceneCount": 4, "tags": []},
            {"id": "5", "name": "Tiny A", "sceneCount": 2, "tags": []},
            {"id": "6", "name": "Tiny B", "sceneCount": 1, "tags": []},
        ]

    def test_tiering_thresholds(self, ):
        rows = autodir.performer_channels(self.performers(), 10, 5)
        kinds = [(r["kind"], r.get("name")) for r in rows]
        assert ("performer", "Star") in kinds
        assert ("performer", "Small Own") in kinds  # 3..group get own channels
        assert ("performer", "Mid Specialized") not in kinds  # mid-band -> group
        spillover = next(r for r in rows if r["kind"] == "performerSpillover")
        assert spillover["members"] == 2
        assert spillover["number"] == 499

    def test_mid_band_specialized_lands_in_matching_group(self):
        rows = autodir.performer_channels(self.performers(), 10, 5)
        inked = [r for r in rows if r["name"] == "Inked Up"]
        assert len(inked) == 1
        assert inked[0]["members"] == 1  # only Mid Inked

    def test_mid_band_unspecialized_lands_in_the_ensemble(self):
        ensemble_number = max(
            s["number"] for s in SPECS["performerGroups"] if not s["aliases"]
        )
        rows = autodir.performer_channels(self.performers(), 10, 5)
        ensemble = next(r for r in rows if r["id"] == f"group:{ensemble_number}")
        assert ensemble["members"] == 1  # only Mid Plain

    def test_solo_channels_ordered_by_scene_count(self):
        rows = autodir.performer_channels(self.performers(), 10, 5)
        solos = [r for r in rows if r["kind"] == "performer"]
        assert [r["name"] for r in solos] == ["Star", "Small Own"]
        assert solos[0]["number"] == 401
        assert solos[1]["number"] == 402


class TestStudioTiers:
    def studios(self):
        return [
            {"id": "1", "name": "Mid Toons", "sceneCount": 7, "tags": ["cartoon"]},
            {"id": "2", "name": "Mid Outdoors", "sceneCount": 7, "tags": []},
            {"id": "3", "name": "Mid Mystery", "sceneCount": 6, "tags": []},
            {"id": "4", "name": "Small Potatoes", "sceneCount": 1, "tags": []},
        ]

    def scene_tags(self, studio_id):
        # "Mid Outdoors" scene tags point at the outdoor genre
        return ["SET: Outdoor", "nature", "outdoor sex"] if studio_id == "2" else ["noir"]

    def test_scoring_assigns_matching_group(self):
        rows = autodir.studio_channels(self.studios(), 10, 5, self.scene_tags)
        toon = next(r for r in rows if r["name"] == "Toon Lagoon")
        assert toon["members"] == 1  # Big Toons (studio tag "cartoon", 25 scenes)
        outdoor = next(r for r in rows if "Outdoors" in r["name"])
        assert outdoor["members"] == 1  # Mid Outdoors via scene sample

    def test_unmatched_mid_studio_goes_to_spillover(self):
        rows = autodir.studio_channels(self.studios(), 10, 5, self.scene_tags)
        spillover = next(r for r in rows if r["kind"] == "studioSpillover")
        assert spillover["name"] == SPECS["studioSpilloverName"]
        assert spillover["members"] == 2  # Mid Mystery ("noir" matches nothing) + Small Potatoes (1 scene)
        assert spillover["number"] == 399

    def test_runner_up_group_included_at_twenty_percent(self):
        # A studio whose tags strongly match two specs pulls both groups.
        studios = [{"id": "1", "name": "Dual", "sceneCount": 7, "tags": ["cartoon", "kids", "SET: Outdoor"]}]
        rows = autodir.studio_channels(studios, 10, 5, lambda _id: [])
        groups = [r for r in rows if r["kind"] == "studioGroup"]
        assert len(groups) == 2  # Toon Lagoon + The Great Outdoors


class TestFullDirectoryWiring:
    def test_build_full_directory_combines_sections(self):
        def submit(query, variables):
            if "findTags" in query:
                return {"findTags": {"tags": [{"id": "1", "name": "SET: Bedroom", "scene_count": 4}]}}
            if "findPerformers" in query:
                return {"findPerformers": {"performers": [
                    {"id": "1", "name": "Star", "scene_count": 30, "tags": []},
                ]}}
            if "findStudios" in query:
                return {"findStudios": {"studios": [
                    {"id": "1", "name": "Mid Toons", "scene_count": 7, "tags": [{"name": "cartoon"}]},
                ]}}
            if "JustWatchStudioSample" in query:
                return {"findScenes": {"scenes": []}}
            raise AssertionError(query)

        result = autodir.build_full_directory(submit, {"soloThreshold": 10, "groupThreshold": 5})
        assert result["general"]["tagChannels"][0]["name"] == "The Boudoir Channel"
        assert result["performers"]["channels"][0]["name"] == "Star"
        assert any(r["name"] == "Toon Lagoon" for r in result["studios"]["channels"])
        # The 7-scene studio is mid-band: it airs via Toon Lagoon, not a solo channel.
        assert result["studios"]["channels"][0]["name"] == "Toon Lagoon"
