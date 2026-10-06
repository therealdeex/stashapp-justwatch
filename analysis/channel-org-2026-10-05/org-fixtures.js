// Fixture library for the channel-organization browser harness. Shapes mirror
// tests/test_arrangement_ui_bridge.py's bridge_doc()/net_chan() — every row
// must pass justwatch.library's STRICT stored-shape validation (real source
// rules, #RRGGBB colors, contract sorts, grp_ group ids), because the mock
// transport runs the REAL Python layer against this document.
'use strict';

const LIBRARY_ID = 'lib_org_harness';
const REVISION = 12;

// Distinct rules so every stored source validates (empty criteria is a typed
// error server-side) and signatures differ per channel.
function source(number) {
  return { type: 'criteria', studiosAny: [String(number)] };
}

function chan(id, kind, number, name, groupId, over) {
  return Object.assign({
    id, kind, number, name,
    glyph: null,
    color: '#3949AB',
    groupId,
    sort: 'newest',
    seed: 1000 + number,
    enabled: true,
    archived: false,
    paused: false,
    source: source(number),
    sourceLabel: '',
    programming: { mode: 'fixed' },
    provenance: { origin: 'v4-final-proposal' },
  }, over || {});
}

// Groups (positions fixed) + channels laid out for the eleven flows:
//   297-302 Alpha packing yard · 171/173/175/180 Beta block yard ·
//   310/311 Performers ("Zulu Net" number-order vs "Alpha Net" name-order —
//   makes arrange_range's order option observable) · 320/450 outsiders ·
//   700-702 Trio (shortfall case) · one ch-band row.
const GROUPS = [
  { id: 'grp_my00001', name: 'My Channels', position: 1, legacySection: null },
  { id: 'grp_alpha001', name: 'Alpha', position: 2, legacySection: null },
  { id: 'grp_beta0001', name: 'Beta', position: 3, legacySection: null },
  { id: 'grp_perfo01', name: 'Performers', position: 4, legacySection: null },
  { id: 'grp_trio001', name: 'Trio', position: 5, legacySection: null },
];

const CHANNELS = [
  chan('ch_00000005', 'ch', 5, 'My Five', 'grp_my00001',
       { source: { type: 'criteria', tagsAny: ['5'] } }),
  chan('net_a0000001', 'net', 297, 'Chart One', 'grp_alpha001'),
  chan('net_a0000002', 'net', 298, 'Chart Two', 'grp_alpha001'),
  chan('net_a0000003', 'net', 299, 'Chart Three', 'grp_alpha001'),
  chan('net_b0000001', 'net', 300, 'Occupied 300', 'grp_alpha001'),
  chan('net_b0000002', 'net', 301, 'Occupied 301', 'grp_alpha001'),
  chan('net_b0000003', 'net', 302, 'Occupied 302', 'grp_alpha001'),
  chan('net_c0000001', 'net', 171, 'Blocker 171', 'grp_beta0001'),
  chan('net_c0000002', 'net', 173, 'Blocker 173', 'grp_beta0001'),
  chan('net_c0000003', 'net', 175, 'Bystander 175', 'grp_beta0001'),
  chan('net_c0000004', 'net', 180, 'Blocker 180', 'grp_beta0001'),
  chan('net_d0000001', 'net', 320, 'Outsider 320', 'grp_beta0001'),
  chan('net_d0000002', 'net', 450, 'Outsider 450', 'grp_beta0001'),
  chan('net_e0000001', 'net', 310, 'Zulu Net', 'grp_perfo01'),
  chan('net_e0000002', 'net', 311, 'Alpha Net', 'grp_perfo01'),
  chan('net_f0000001', 'net', 700, 'Trio One', 'grp_trio001'),
  chan('net_f0000002', 'net', 701, 'Trio Two', 'grp_trio001'),
  chan('net_f0000003', 'net', 702, 'Trio Three', 'grp_trio001'),
];

function buildFixture() {
  return {
    schemaVersion: 1,
    libraryId: LIBRARY_ID,
    revision: REVISION,
    groups: GROUPS.map((g) => Object.assign({}, g)),
    channels: CHANNELS.map((c) => Object.assign({}, c)),
    recentRequests: [],
  };
}

// Stash entity stubs for the picker/name-resolution GraphQL queries.
const ENTITIES = {
  tags: Array.from({ length: 12 }, (_, i) => ({ id: String(i + 1), name: 'Tag ' + (i + 1) })),
  performers: Array.from({ length: 8 }, (_, i) => ({ id: String(i + 1), name: 'Performer ' + (i + 1) })),
  studios: Array.from({ length: 900 }, (_, i) => ({ id: String(i + 1), name: 'Studio ' + (i + 1) })),
};

module.exports = { LIBRARY_ID, REVISION, buildFixture, ENTITIES };
