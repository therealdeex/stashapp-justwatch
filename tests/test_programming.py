import copy
from pathlib import Path

from justwatch import programming as p, snapshots


def channel(**cfg):
    return {"id": "ch_12345678", "source": {"type": "tag", "id": "1"}, "sort": "shuffle", "seed": 17,
            "programming": {"mode": "explore", **cfg}}


def entries(n=100, duration=3600):
    return [{"id": str(i), "title": f"Program {i}", "duration": duration, "studioId": str(i % 5),
             "studio": f"Studio {i % 5}", "performerIds": [str(i % 7)]} for i in range(n)]


def test_complete_deck_before_repetition():
    result = p.build(channel(), entries(60), now=0)
    ids = [a["item"]["id"] for a in result["programs"]]
    assert len(set(ids[:60])) == 60
    assert len(result["programs"]) == 72
    assert len(set(ids[60:])) == 12


def test_replenishment_preserves_published_airings_and_pending_deck():
    first = p.build(channel(), entries(200), now=0)
    second = p.build(channel(), entries(200), first, now=24 * p.HOUR)
    assert second["programs"][:72] == first["programs"]
    assert len({a["item"]["id"] for a in second["programs"]}) == 96
    assert second["preparedThrough"] == 96 * p.HOUR


def test_pure_determinism_and_input_not_mutated():
    a = p.build(channel(), entries(), now=0)
    prior = copy.deepcopy(a)
    assert p.build(channel(), entries(), a, now=p.HOUR) == p.build(channel(), entries(), a, now=p.HOUR)
    assert a == prior


def test_configuration_edit_finishes_current_program():
    old = p.build(channel(), entries(), now=0)
    changed = channel(spacing=2)
    result = p.build(changed, entries(), old, now=p.HOUR // 2)
    assert result["programs"][0] == old["programs"][0]
    assert result["programs"][1]["startEpochMs"] == p.HOUR


def test_branding_does_not_replace_programming():
    first = p.build(channel(), entries(), now=0)
    renamed = dict(channel(), name="New name", color="#123456", number=4)
    result = p.build(renamed, entries(), first, now=0)
    assert result["version"] == first["version"]


def test_spacing_and_repeat_constraints_relax_for_small_source():
    result = p.build(channel(spacing=5, repeatHours=168), entries(1), now=0)
    assert len(result["programs"]) == 72
    assert result["warnings"]


def test_spacing_separates_performers_and_studios_when_possible():
    result = p.build(channel(spacing=1), entries(100), now=0)
    for a, b in zip(result["programs"], result["programs"][1:]):
        assert a["item"]["studioId"] != b["item"]["studioId"]
        assert not set(a["item"]["performerIds"]) & set(b["item"]["performerIds"])


def test_schedule_page_boundary_and_fallback(tmp_path):
    data = p.build(channel(), entries(), now=0)
    snapshots.write_json(p.path(tmp_path, channel()["id"]), data)
    first = p.schedule(tmp_path, channel()["id"], at=p.HOUR, limit=2)
    assert len(first["programs"]) == 2
    assert first["programs"][0]["startEpochMs"] == p.HOUR
    late = p.schedule(tmp_path, channel()["id"], at=100 * p.HOUR, limit=3)
    assert late["status"] == "repeat"
    again = p.schedule(tmp_path, channel()["id"], at=100 * p.HOUR, limit=3)
    late.pop("serverNow"), again.pop("serverNow")  # live clock; not part of the determinism contract
    assert late == again
    assert late["programs"][0]["startEpochMs"] <= 100 * p.HOUR < late["programs"][0]["endEpochMs"]


def test_empty_source_has_no_invented_filler():
    data = p.build(channel(), [], now=0)
    assert data["programs"] == []


def test_spotlight_keeps_source_membership_and_groups_two_programs():
    # Epoch is Thursday, first airing can start at 00:00 UTC.
    data = p.build(channel(spotlight="studio", spotlightDay=3, spotlightHour=0), entries(), now=0)
    a, b = data["programs"][:2]
    assert a["block"] and b["block"]
    assert a["item"]["studioId"] == b["item"]["studioId"]
    assert set(x["item"]["id"] for x in data["programs"]) <= set(x["id"] for x in entries())


def test_readers_never_advance_deck(tmp_path):
    data = p.build(channel(), entries(), now=0)
    file = p.path(tmp_path, channel()["id"])
    snapshots.write_json(file, data)
    before = file.read_bytes()
    p.schedule(tmp_path, channel()["id"], at=20 * p.HOUR)
    assert file.read_bytes() == before


def test_index_pages_beyond_old_scan_cap():
    class Client:
        def submit(self, query, variables):
            assert variables["scene_filter"] == {"tags": {"value": ["1"], "modifier": "INCLUDES", "depth": -1}}
            page = variables["filter"]["page"]
            return {"findScenes": {"count": 1500, "scenes": [
                {"id": str(i), "title": "Demo", "files": [{"duration": 60}], "performers": []}
                for i in range((page - 1) * 250, page * 250)]}}
    assert len(p.index_source(Client(), channel())) == 1500


def test_discovery_prioritizes_unscheduled_scenes():
    previous = {"airCounts": {str(i): 100 for i in range(50)}, "lastScheduled": {}}
    result = p.build(channel(mode="discovery"), entries(100), previous, now=0)
    assert all(int(a["item"]["id"]) >= 50 for a in result["programs"][:50])


def test_invalid_channel_path_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        p.read(tmp_path, "../catalog")


def test_twenty_thousand_scene_library_replenishes_without_repeating():
    library = entries(20_000)
    first = p.build(channel(), library, now=0)
    second = p.build(channel(), library, first, now=24 * p.HOUR)
    assert second['sourceTotal'] == 20_000
    assert len(set(a['item']['id'] for a in second['programs'])) == len(second['programs'])
    assert len(second['deck']) == 20_000 - len(second['programs'])


def test_outage_recovery_finishes_current_encore(tmp_path):
    old = p.build(channel(), entries(), now=0)
    snapshots.write_json(p.path(tmp_path, channel()['id']), old)
    now = 200 * p.HOUR + p.HOUR // 2
    encore = p.schedule(tmp_path, channel()['id'], at=now)['programs'][0]
    recovered = p.build(channel(), entries(), old, now=now)
    current = next(a for a in recovered['programs'] if a['startEpochMs'] <= now < a['endEpochMs'])
    assert current == encore
    following = next(a for a in recovered['programs'] if a['startEpochMs'] == current['endEpochMs'])
    assert following['endEpochMs'] > following['startEpochMs']


def test_draft_preview_does_not_publish_or_advance_deck(tmp_path):
    old = p.build(channel(), entries(), now=0)
    file = p.path(tmp_path, channel()['id'])
    snapshots.write_json(file, old)
    before = file.read_bytes()
    result = p.preview(tmp_path, channel(spacing=2), now=p.HOUR // 2)
    assert result['status'] == 'preview'
    assert result['effectiveAt'] == p.HOUR
    assert result['programs'][0] == old['programs'][0]
    assert file.read_bytes() == before


def test_preview_accepts_publications_stored_before_multi_tag(tmp_path):
    old = p.build(channel(), entries(), now=0)
    legacy = copy.deepcopy(old)
    legacy['source'] = {"type": "tag", "id": "1"}  # stored before tag sets existed
    snapshots.write_json(p.path(tmp_path, channel()['id']), legacy)
    draft = channel()
    draft['source'] = {"type": "tag", "id": "1", "ids": ["1"]}
    result = p.preview(tmp_path, draft, now=p.HOUR // 2)
    assert result['status'] == 'preview'


def test_source_change_cannot_preview_using_old_membership(tmp_path):
    snapshots.write_json(p.path(tmp_path, channel()['id']), p.build(channel(), entries(), now=0))
    draft = channel()
    draft['source'] = {'type': 'studio', 'id': '5'}
    assert p.preview(tmp_path, draft, now=0)['status'] == 'needsIndex'


def test_short_program_edit_gives_clients_notice(tmp_path):
    old = p.build(channel(), entries(200, duration=60), now=0)
    result = p.build(channel(spacing=1), entries(200, duration=60), old, now=30_000)
    # Preserve all whole airings through at least two minutes of notice.
    assert result['programs'][:3] == old['programs'][:3]
    assert result['programs'][3]['startEpochMs'] == 180_000
