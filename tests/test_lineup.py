"""Source projections: tag sets, rotation identity, source resolution."""

from justwatch import lineup


class Client:
    """Minimal GraphQL stub: findTag names keyed by id."""

    def __init__(self, tags):
        self.tags = tags

    def submit(self, query, variables):
        return {"findTag": {"name": self.tags.get(variables["id"], "")}}


def test_single_tag_projection_is_unchanged():
    assert lineup.build_scene_filter({"type": "tag", "id": "42"}) == \
        {"tags": {"value": ["42"], "modifier": "INCLUDES", "depth": -1}}


def test_tag_set_projects_every_id():
    source = {"type": "tag", "id": "3", "ids": ["3", "9", "12"]}
    assert lineup.build_scene_filter(source)["tags"]["value"] == ["3", "9", "12"]


def test_legacy_tag_source_without_ids_still_projects():
    assert lineup.tag_ids({"type": "tag", "id": "7"}) == ["7"]


def test_rotation_version_distinguishes_tag_sets():
    wide = {"type": "tag", "id": "3", "ids": ["3", "9"]}
    narrow = {"type": "tag", "id": "3", "ids": ["3"]}
    assert lineup.rotation_version(wide, "shuffle", 17, 50) != \
        lineup.rotation_version(narrow, "shuffle", 17, 50)
    assert lineup.rotation_version(wide, "shuffle", 17, 50) == \
        lineup.rotation_version({"type": "tag", "id": "3", "ids": ["9", "3"]}, "shuffle", 17, 50)


def test_source_key_matches_legacy_publications():
    assert lineup.source_key({"type": "tag", "id": "7"}) == \
        lineup.source_key({"type": "tag", "id": "7", "ids": ["7"]})


def test_resolve_tag_set_labels_first_two_with_overflow():
    client = Client({"3": "kids", "9": "comedy", "12": "toons"})
    label, exists = lineup.resolve_source(client, {"type": "tag", "id": "3", "ids": ["3", "9", "12"]})
    assert (label, exists) == ("kids, comedy +1", True)


def test_resolve_tag_set_exists_when_any_tag_exists():
    client = Client({"3": "kids"})
    label, exists = lineup.resolve_source(client, {"type": "tag", "id": "3", "ids": ["3", "404"]})
    assert (label, exists) == ("kids", True)


def test_resolve_tag_set_missing_when_all_tags_gone():
    label, exists = lineup.resolve_source(Client({}), {"type": "tag", "id": "3", "ids": ["3", "9"]})
    assert (label, exists) == ("", False)
