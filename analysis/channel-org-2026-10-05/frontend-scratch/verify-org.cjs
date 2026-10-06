// Phase-3 mock-driven harness for the channel-organization frontend.
// Serves the REAL ui/index.js + ui/styles.css in headless Chromium against a
// mock GraphQL backend implementing the FROZEN contract (one planner, one
// preview op, packet object envelope {expectedRevision, ops}, typed errors).
// Flows: capability gating, create-at-occupied-300 (resolution sheet), block
// move, arrange range (useAvailable + exclusive), undo/redo, revision
// conflict -> Reload and replan.
//
//   node verify-org.cjs
// Deps (env overrides): JW_AUDIT_PLAYWRIGHT, JW_AUDIT_REACT_ROOT, JW_AUDIT_CHROME.
const fs = require('fs');
const path = require('path');
const {chromium} = require(process.env.JW_AUDIT_PLAYWRIGHT || '/tmp/stash-verify/node_modules/playwright-core');
const root = path.resolve(__dirname, '../../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';

const clone = (x) => JSON.parse(JSON.stringify(x));
const BANDS = {ch: [1, 99], net: [100, 899]};

// ---------------- fixture ----------------
function freshDoc() {
  const mk = (id, kind, number, name, groupId) => ({
    id, kind, number, name, glyph: null, color: '#3949AB', groupId, sort: 'shuffle',
    seed: number, enabled: true, archived: false, paused: false,
    source: {type: 'criteria', tagsAny: ['1']}, sourceLabel: 'Rules',
    programming: {mode: 'fixed'}, provenance: {origin: 'custom'},
  });
  return {
    libraryId: 'lib_org', revision: 12,
    groups: [
      {id: 'grp_my', name: 'My Channels', position: 1},
      {id: 'grp_net', name: 'Networks', position: 2},
      {id: 'grp_perf', name: 'Performers', position: 3},
    ],
    channels: [
      mk('ch_00000001', 'ch', 1, 'My One', 'grp_my'),
      mk('ch_00000002', 'ch', 2, 'My Two', 'grp_my'),
      mk('net_00000171', 'net', 171, 'Block One', 'grp_net'),
      mk('net_00000173', 'net', 173, 'Block Two', 'grp_net'),
      mk('net_00000180', 'net', 180, 'Block Three', 'grp_net'),
      mk('net_00000300', 'net', 300, 'Taken A', 'grp_net'),
      mk('net_00000301', 'net', 301, 'Taken B', 'grp_net'),
      mk('net_00000302', 'net', 302, 'Taken C', 'grp_net'),
      mk('net_00000305', 'net', 305, 'Taken D', 'grp_net'),
      mk('net_00000320', 'net', 320, 'Outsider One', 'grp_net'),
      mk('net_00000450', 'net', 450, 'Outsider Two', 'grp_net'),
      mk('net_00000400', 'net', 400, 'Perf One', 'grp_perf'),
      mk('net_00000401', 'net', 401, 'Perf Two', 'grp_perf'),
      mk('net_00000402', 'net', 402, 'Perf Three', 'grp_perf'),
      mk('net_00000403', 'net', 403, 'Perf Four', 'grp_perf'),
    ],
  };
}

// ---------------- mock planner (frozen contract) ----------------
let doc = freshDoc();
let idCounter = 0;

const byNum = (d) => { const m = new Map(); for (const c of d.channels) m.set(c.number, c); return m; };
const bandOf = (kind) => BANDS[kind] || BANDS.net;

function firstFreeAtOrAbove(occ, n, kind) {
  const [lo, hi] = bandOf(kind);
  for (let i = Math.max(n, lo); i <= hi; i++) if (!occ.has(i)) return i;
  return null;
}
function firstFreeAtOrBelow(occ, n, kind) {
  const [lo] = bandOf(kind);
  for (let i = n; i >= lo; i--) if (!occ.has(i)) return i;
  return null;
}

function plan(intent, overlays) {
  const errs = [];
  const numberChanges = [];
  const groupChanges = [];
  const created = [];
  const warnings = [];
  const ov = new Map((overlays || []).map((o) => [o.tempRef, o]));
  const occ = byNum(doc);
  const occNums = new Set(occ.keys());
  const byId = new Map(doc.channels.map((c) => [c.id, c]));

  const chId = intent.channelId || null;
  const tRef = intent.tempRef || null;
  const mover = chId ? byId.get(chId) : null;
  const moverKind = mover ? mover.kind : (tRef && ov.get(tRef) ? ov.get(tRef).kind : 'net');
  const selRef = chId || tRef;

  const pushRenumber = (id, from, to, selected, reason) => {
    numberChanges.push({channelId: id.startsWith('temp-') ? null : id, tempRef: id.startsWith('temp-') ? id : null,
      from, to, selected, reason});
  };

  if (intent.type === 'insert' || intent.type === 'free_insert') {
    const n = intent.number;
    if (mover) occNums.delete(mover.number);
    const target = n;
    if (!occNums.has(target)) {
      if (mover) pushRenumber(mover.id, mover.number, target, true, 'direct');
      else created.push({tempRef: tRef, number: target, groupId: (ov.get(tRef) || {}).groupId || null});
    } else if (intent.direction === 'down') {
      const F = firstFreeAtOrBelow(occNums, target, moverKind);
      if (F == null) errs.push({path: 'intent.number', code: 'no_capacity', message: 'No free number below ' + bandOf(moverKind)[0] + '.'});
      else {
        for (let i = F; i < target; i++) {
          const c = occ.get(i);
          if (c) { pushRenumber(c.id, i, i - 1, false, 'insert-shift-down'); occNums.delete(i); occNums.add(i - 1); occ.set(i - 1, c); occ.delete(i); }
        }
        if (mover) pushRenumber(mover.id, mover.number, target, true, 'direct');
        else created.push({tempRef: tRef, number: target, groupId: (ov.get(tRef) || {}).groupId || null});
      }
    } else {
      const F = firstFreeAtOrAbove(occNums, target, moverKind);
      if (F == null) errs.push({path: 'intent.number', code: 'no_capacity', message: 'No free number above ' + bandOf(moverKind)[1] + '.'});
      else {
        for (let i = F - 1; i >= target; i--) {
          const c = occ.get(i);
          if (c) { pushRenumber(c.id, i, i + 1, false, 'insert-shift-up'); occNums.delete(i); occNums.add(i + 1); occ.delete(i); occ.set(i + 1, c); }
        }
        if (mover) pushRenumber(mover.id, mover.number, target, true, 'direct');
        else created.push({tempRef: tRef, number: target, groupId: (ov.get(tRef) || {}).groupId || null});
      }
    }
  } else if (intent.type === 'swap') {
    const a = byId.get(intent.a); const b = byId.get(intent.b);
    if (!a || !b) errs.push({path: 'intent', code: 'unknown_channel', message: 'Unknown channel.'});
    else {
      pushRenumber(a.id, a.number, b.number, true, 'swap');
      pushRenumber(b.id, b.number, a.number, true, 'swap');
    }
  } else if (intent.type === 'relocate_occupant') {
    const c = byId.get(intent.channelId);
    if (!c) errs.push({path: 'intent.channelId', code: 'unknown_channel', message: 'Unknown channel.'});
    else if (c.number === intent.to) { /* noop */ }
    else if (occNums.has(intent.to)) {
      const holder = occ.get(intent.to);
      errs.push({path: 'intent.to', code: 'destination_occupied',
        message: intent.to + ' is held by "' + holder.name + '" (' + holder.id + ').'});
    } else pushRenumber(c.id, c.number, intent.to, true, 'relocate');
  } else if (intent.type === 'move_block') {
    const ids = (intent.channelIds || []).concat(intent.tempRefs || []);
    if (!ids.length) errs.push({path: 'intent', code: 'empty_selection', message: 'Nothing selected.'});
    const kind = moverKindOfSelection(ids, byId, ov);
    const selSet = new Set(ids);
    // occupancy of UNSELECTED rows only; the block is placed on top, then
    // displaced bystanders cascade upward in original relative order.
    const free = new Set();
    {
      const [lo, hi] = bandOf(kind);
      const taken = new Set(doc.channels.filter((c) => !selSet.has(c.id)).map((c) => c.number));
      for (let i = lo; i <= hi; i++) if (!taken.has(i)) free.add(i);
    }
    const blockSlots = new Set();
    ids.forEach((id, i) => {
      const to = intent.start + i;
      const c = byId.get(id);
      if (c) { if (c.number !== to) pushRenumber(id, c.number, to, true, 'direct'); }
      else created.push({tempRef: id, number: to, groupId: (ov.get(id) || {}).groupId || null});
      blockSlots.add(to);
      free.delete(to);
    });
    const nextFree = (from) => {
      for (let i = from; ; i++) {
        if (free.has(i)) return i;
        if (i > bandOf(kind)[1]) return null;
      }
    };
    let cursor = intent.start + ids.length;
    for (const c of doc.channels.slice().sort((x, y) => x.number - y.number)) {
      if (selSet.has(c.id)) continue;
      if (!blockSlots.has(c.number)) continue; // its slot survived
      const placed = nextFree(cursor);
      if (placed == null) { errs.push({path: 'intent.start', code: 'no_capacity', message: 'The block does not fit above ' + intent.start + '.'}); break; }
      pushRenumber(c.id, c.number, placed, false, 'block-displaced');
      free.delete(placed);
      cursor = placed + 1;
    }
  } else if (intent.type === 'arrange_range') {
    const ids = (intent.channelIds || []).concat(intent.tempRefs || []);
    if (!ids.length) errs.push({path: 'intent', code: 'empty_selection', message: 'Nothing selected.'});
    const kinds = new Set(ids.map((id) => { const c = byId.get(id); return c ? c.kind : (ov.get(id) || {}).kind || 'net'; }));
    const r = intent.range;
    const rangeKind = r.start >= 100 ? 'net' : 'ch';
    if (kinds.size > 1 || (kinds.size === 1 && [...kinds][0] !== rangeKind)) {
      errs.push({path: 'intent.range', code: 'band_mixed',
        message: 'The selection mixes kinds for ' + r.start + '–' + r.end + ': ' + ids.join(', ')});
    }
    const selSet = new Set(ids);
    for (const id of ids) { const c = byId.get(id); if (c) occNums.delete(c.number); }
    const outsiders = doc.channels.filter((c) => !selSet.has(c.id) && c.number >= r.start && c.number <= r.end)
      .sort((a, b) => a.number - b.number);
    const freeInRange = [];
    for (let i = r.start; i <= r.end; i++) if (!occNums.has(i) && !outsiders.some((o) => o.number === i)) freeInRange.push(i);
    if (intent.strategy === 'exclusive') {
      const out = intent.outside;
      if (!out) errs.push({path: 'intent.outside', code: 'bad_range', message: 'An exclusive arrangement needs an outside interval.'});
      else {
        const outsideFree = [];
        for (let i = out.start; i <= out.end; i++) if (!occNums.has(i)) outsideFree.push(i);
        if (outsideFree.length < outsiders.length) {
          errs.push({path: 'intent.outside', code: 'no_capacity',
            message: outsiders.length + ' outsiders but only ' + outsideFree.length + ' free numbers outside.'});
        } else {
          outsiders.forEach((o, i) => {
            pushRenumber(o.id, o.number, outsideFree[i], false, 'exclusive-outside-relocation');
            occNums.delete(o.number); occNums.add(outsideFree[i]);
            freeInRange.push(o.number);
          });
          freeInRange.sort((a, b) => a - b);
        }
      }
    }
    if (freeInRange.length < ids.length) {
      errs.push({path: 'intent.range', code: 'no_capacity',
        message: ids.length + ' channels into ' + freeInRange.length + ' free slots — ' + (ids.length - freeInRange.length) + ' short.'});
    } else {
      const ordered = ids.slice().sort((a, b) => {
        const ca = byId.get(a); const cb = byId.get(b);
        if (intent.order === 'name') {
          const na = (ca ? ca.name : (ov.get(a) || {}).name || a).toLowerCase();
          const nb = (cb ? cb.name : (ov.get(b) || {}).name || b).toLowerCase();
          return na.localeCompare(nb) || String(a).localeCompare(String(b));
        }
        return (ca ? ca.number : 1e9) - (cb ? cb.number : 1e9) || String(a).localeCompare(String(b));
      });
      ordered.forEach((id, i) => {
        const to = freeInRange[i];
        const c = byId.get(id);
        if (c) { if (c.number !== to) pushRenumber(id, c.number, to, true, 'range-pack'); }
        else created.push({tempRef: id, number: to, groupId: (ov.get(id) || {}).groupId || null});
      });
    }
  } else if (intent.type === 'shift_interval') {
    const r = intent.range;
    const movers = doc.channels.filter((c) => c.number >= r.start && c.number <= r.end).sort((a, b) => a.number - b.number);
    if (movers.length) {
      const moverSet = new Set(movers.map((c) => c.id));
      const moverNums = new Set(movers.map((c) => c.number + intent.offset));
      for (const c of movers) {
        const to = c.number + intent.offset;
        const [lo, hi] = bandOf(c.kind);
        if (to < lo || to > hi) { errs.push({path: 'intent.offset', code: 'cross_band', message: c.name + ' would land at ' + to + '.'}); break; }
        const holder = occ.get(to);
        if (holder && !moverSet.has(holder.id) && !moverNums.has(to)) {
          errs.push({path: 'intent.range', code: 'range_overlap',
            message: to + ' is held by "' + holder.name + '" (' + holder.id + ').'});
          break;
        }
      }
      if (!errs.length) for (const c of movers) pushRenumber(c.id, c.number, c.number + intent.offset, true, 'shift-interval');
    }
  } else if (intent.type === 'assign_group') {
    const toG = intent.groupId || ('grp_mock_' + (++idCounter));
    if (intent.createGroup) groupCreates.push({id: toG, name: intent.createGroup.name});
    for (const id of (intent.channelIds || [])) {
      const c = byId.get(id);
      if (c && c.groupId !== toG) groupChanges.push({channelId: id, tempRef: null, from: c.groupId, to: toG});
    }
    for (const ref of (intent.tempRefs || [])) {
      const o = ov.get(ref) || {};
      created.push({tempRef: ref, number: null, groupId: toG});
      void o;
    }
  }

  const valid = errs.length === 0;
  const noop = valid && !numberChanges.length && !groupChanges.length
    && !created.some((c) => c.number != null) && !(intent.type === 'assign_group' && created.length);
  // packet: group.put -> channel.create skeletons -> channels.move -> channels.renumber
  const ops = [];
  for (const g of groupCreates.splice(0)) ops.push({op: 'group.put', group: {id: g.id, name: g.name, position: doc.groups.length + 1}});
  for (const cr of created) {
    const o = ov.get(cr.tempRef) || {};
    const skel = {kind: o.kind || 'net'};
    if (cr.number != null) skel.number = cr.number;
    if (o.name) skel.name = o.name;
    if (cr.groupId || o.groupId) skel.groupId = cr.groupId || o.groupId;
    ops.push({op: 'channel.create', tempId: cr.tempRef, channel: skel});
  }
  if (groupChanges.length) {
    const byG = new Map();
    for (const gc of groupChanges) { const l = byG.get(gc.to) || []; l.push(gc.channelId); byG.set(gc.to, l); }
    for (const [gid, ids] of byG) ops.push({op: 'channels.move', channelIds: ids, groupId: gid});
  }
  const assignments = numberChanges.filter((nc) => nc.channelId).map((nc) => ({channelId: nc.channelId, number: nc.to}));
  if (assignments.length) ops.push({op: 'channels.renumber', assignments});

  const displaced = numberChanges.filter((nc) => !nc.selected);
  return {
    pluginId: 'stash-justwatch', contractVersion: 1,
    revision: doc.revision, libraryId: doc.libraryId,
    noop, numberChanges, groupChanges, created, displaced,
    capacity: intent.type === 'arrange_range' ? {
      kind: rangeKindOf(intent), bandStart: 100, bandEnd: 899, totalSlots: 800,
      occupiedSlots: doc.channels.length, freeSlots: 800 - doc.channels.length,
      neededSlots: (intent.channelIds || []).length + (intent.tempRefs || []).length,
      shortfall: errs.some((e) => e.code === 'no_capacity') ? 1 : 0,
      range: {start: intent.range.start, end: intent.range.end,
        totalSlots: intent.range.end - intent.range.start + 1,
        occupiedSlots: doc.channels.filter((c) => c.number >= intent.range.start && c.number <= intent.range.end).length,
        freeSlots: 0, outsiderSlots: displaced.length ? displaced.length : doc.channels.filter((c) => c.number >= intent.range.start && c.number <= intent.range.end && !((intent.channelIds || []).includes(c.id))).length},
      outside: intent.outside ? {start: intent.outside.start, end: intent.outside.end,
        totalSlots: intent.outside.end - intent.outside.start + 1,
        occupiedSlots: 0, freeSlots: intent.outside.end - intent.outside.start + 1 - doc.channels.filter((c) => c.number >= intent.outside.start && c.number <= intent.outside.end).length} : null,
    } : null,
    suggestions: null, warnings, errors: errs, valid,
    packet: valid && !noop ? {expectedRevision: doc.revision, ops} : null,
  };
}
const groupCreates = [];
function rangeKindOf(intent) { return intent.range && intent.range.start >= 100 ? 'net' : 'ch'; }
function moverKindOfSelection(ids, byId, ov) {
  for (const id of ids) { const c = byId.get(id); if (c) return c.kind; }
  for (const id of ids) { const o = ov.get(id); if (o) return o.kind || 'net'; }
  return 'net';
}

function explainChoices(intent, overlays) {
  const n = intent.number;
  const occ = byNum(doc);
  const occNums = new Set(occ.keys());
  const byId = new Map(doc.channels.map((c) => [c.id, c]));
  const mover = intent.channelId ? byId.get(intent.channelId) : null;
  if (mover) occNums.delete(mover.number);
  const kind = mover ? mover.kind : ((overlays || []).find((o) => o.tempRef === intent.tempRef) || {}).kind || 'net';
  const holder = occ.get(n);
  const freeList = [];
  {
    const [lo, hi] = bandOf(kind);
    for (let d = 0; freeList.length < 5 && d <= hi - lo; d++) {
      if (n + d <= hi && !occNums.has(n + d)) freeList.push(n + d);
      else if (n - d >= lo && d > 0 && !occNums.has(n - d)) freeList.push(n - d);
    }
  }
  const upF = firstFreeAtOrAbove(occNums, n, kind);
  const downF = firstFreeAtOrBelow(occNums, n, kind);
  let movedUp = 0;
  if (upF != null) for (let i = n; i < upF; i++) if (occNums.has(i)) movedUp++;
  let movedDown = 0;
  if (downF != null) for (let i = downF + 1; i <= n; i++) if (occNums.has(i)) movedDown++;
  return {
    free: freeList.length ? {nextHigher: upF, nearest: freeList[0], firstFree: bandOf(kind)[0], list: freeList} : null,
    swap: mover
      ? (holder ? {available: true, with: holder.id, withName: holder.name, withNumber: holder.number} : {available: false, reason: 'The destination is free — nothing to swap with.'})
      : {available: false, reason: 'A new channel has no original number to swap into.'},
    shiftUp: upF != null ? {available: true, firstFree: upF, movedCount: movedUp} : {available: false, reason: 'No free number above ' + bandOf(kind)[1] + '.'},
    shiftDown: downF != null ? {available: true, firstFree: downF, movedCount: movedDown} : {available: false, reason: 'No free number below ' + bandOf(kind)[0] + '.'},
    relocate: holder ? {available: true} : {available: false, reason: 'The destination is free — nothing to relocate.'},
  };
}

// ---------------- mock apply ----------------
function applyOps(expected, ops, requestId, receipts) {
  if (Number(expected) !== doc.revision) {
    receipts[requestId] = {requestId, status: 'rejected', error: 'revision_conflict', currentRevision: doc.revision};
    return;
  }
  const cand = clone(doc);
  const idMap = {};
  for (const op of ops) {
    if (op.op === 'group.put') {
      if (!cand.groups.some((g) => g.id === op.group.id)) cand.groups.push(clone(op.group));
      else cand.groups = cand.groups.map((g) => (g.id === op.group.id ? clone(op.group) : g));
    } else if (op.op === 'channel.create') {
      const id = (op.channel.kind === 'ch' ? 'ch_' : 'net_') + (0x90000000 + (++idCounter)).toString(16);
      idMap[op.tempId] = id;
      cand.channels.push(Object.assign(clone(op.channel), {id, seed: idCounter, provenance: {origin: 'custom'}}));
    } else if (op.op === 'channel.put') {
      cand.channels = cand.channels.map((c) => (c.id === op.channel.id ? Object.assign(clone(op.channel), {seed: c.seed}) : c));
    } else if (op.op === 'channels.move') {
      cand.channels = cand.channels.map((c) => (op.channelIds.includes(c.id) ? Object.assign(c, {groupId: op.groupId}) : c));
    } else if (op.op === 'channels.renumber') {
      for (const a of op.assignments) {
        const c = cand.channels.find((x) => x.id === a.channelId);
        if (c) c.number = a.number;
      }
    } else if (op.op === 'channels.patch') {
      cand.channels = cand.channels.map((c) => (op.channelIds.includes(c.id) ? Object.assign(c, clone(op.patch)) : c));
    }
  }
  // final occupancy check (the server's belt-and-braces)
  const seen = new Map();
  for (const c of cand.channels) {
    if (seen.has(c.number)) {
      receipts[requestId] = {requestId, status: 'rejected', error: 'validation_failed',
        errors: [{path: 'ops', code: 'duplicate_number', message: 'Number ' + c.number + ' ends on both "' + seen.get(c.number) + '" and "' + c.name + '".'}]};
      return;
    }
    seen.set(c.number, c.name);
  }
  cand.channels.sort((a, b) => a.number - b.number);
  cand.revision++;
  doc = cand;
  receipts[requestId] = {requestId, status: 'committed', revision: doc.revision, idMap, refresh: {}};
}

// ---------------- page plumbing (pattern from ui-polish harness) ----------------
const FAS = {
  faTags: {iconName: 'tags', icon: [512, 512, [], 'f02c']},
  faUser: {iconName: 'user', icon: [448, 512, [], 'f007']},
  faBuilding: {iconName: 'building', icon: [384, 512, [], 'f1ad']},
  faTrashAlt: {iconName: 'trash-alt', icon: [448, 512, [], 'f2ed']},
};

let ARRANGEMENT_ON = true;

function makePage() {
  const scripts = {
    '/react.js': fs.readFileSync(path.join(deps, 'react/umd/react.development.js')),
    '/react-dom.js': fs.readFileSync(path.join(deps, 'react-dom/umd/react-dom.development.js')),
    '/ui.js': fs.readFileSync(path.join(root, 'ui/index.js')),
    '/ui.css': fs.readFileSync(path.join(root, 'ui/styles.css')),
  };
  const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/ui.css">
<style>:root{--body-color:#202b33;--text-color:#f0f0f5;--primary:#6caddf;--danger:#d9534f;}</style>
<body style="background:var(--body-color);margin:0"><div id="root"></div>
<script src="/react.js"></script><script src="/react-dom.js"></script>
<script>
window.PluginApi = {React, libraries:{FontAwesomeSolid: ${JSON.stringify(FAS)}},
  components:{Icon: function Icon(p){ return React.createElement('span', {className:p.className, style:p.style}, ''); }},
  register:{route(p,c){window.AuditApp=c}}};
</script>
<script src="/ui.js"></script>
<script>ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(window.AuditApp));</script>`;
  return {scripts, html};
}

async function boot(browser, {arrangement = true} = {}) {
  doc = freshDoc();
  idCounter = 0;
  ARRANGEMENT_ON = arrangement;
  const {scripts, html} = makePage();
  const context = await browser.newContext({viewport: {width: 1500, height: 950}});
  const page = await context.newPage();
  const state = {errors: [], previews: [], tasks: [], receipts: {}};
  page.on('pageerror', (e) => state.errors.push(e.message));
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/graphql') {
      const body = route.request().postDataJSON(), vars = body.variables || {};
      let data = {};
      if (body.query.includes('runPluginTask')) {
        const a = vars.a;
        state.tasks.push(clone(a));
        applyOps(a.expectedRevision, JSON.parse(a.ops), a.requestId, state.receipts);
        data = {runPluginTask: 'job-1'};
      } else if (body.query.includes('runPluginOperation')) {
        const a = vars.args || {};
        let out;
        switch (a.mode) {
          case 'Capabilities':
            out = {pluginId: 'stash-justwatch', contractVersion: 1, operations: [],
              features: Object.assign({channelLibrary: {version: 1}, channelGroups: {version: 1}},
                ARRANGEMENT_ON ? {arrangement: {version: 1, previewOperation: 'PreviewChannelArrangement', renumberOpcode: 'channels.renumber'}} : {}),
              limits: {libraryChannels: 899}};
            break;
          case 'GetChannelLibrary': out = Object.assign(clone(doc), {total: doc.channels.length}); break;
          case 'GetChannelDefinition': {
            const cch = doc.channels.find((c) => c.id === a.channelId);
            out = {revision: doc.revision, channel: cch ? clone(cch) : null, summary: []}; break;
          }
          case 'PreviewChannelArrangement': {
            const intent = JSON.parse(a.intent);
            const overlays = a.channels ? JSON.parse(a.channels) : [];
            state.previews.push(clone(intent));
            if (Number(a.expectedRevision) !== doc.revision) {
              out = {revision: doc.revision, correlationToken: a.correlationToken, valid: false, noop: false,
                numberChanges: [], groupChanges: [], created: [], displaced: [], capacity: null, suggestions: null,
                warnings: [], errors: [{path: 'expectedRevision', code: 'stale_revision', message: 'The library moved.', currentRevision: doc.revision}],
                packet: null};
            } else {
              out = plan(intent, overlays);
              out.correlationToken = a.correlationToken;
              out.choices = a.explain && intent.type === 'insert' ? explainChoices(intent, overlays) : null;
            }
            break;
          }
          case 'PreviewChannelPool':
            out = {status: 'ok', poolCount: 128, rotationSize: 50, rotationComplete: true, sample: []}; break;
          case 'ValidateChannelChanges': {
            const cand = clone(doc);
            for (const op of JSON.parse(a.ops)) {
              if (op.op === 'channels.renumber') for (const asg of op.assignments) {
                const c = cand.channels.find((x) => x.id === asg.channelId);
                if (c) c.number = asg.number;
              }
              if (op.op === 'channel.create') cand.channels.push(Object.assign(clone(op.channel), {id: 'tmp'}));
            }
            const seen = new Set(); let bad = null;
            for (const c of cand.channels) { if (seen.has(c.number)) { bad = c.number; break; } seen.add(c.number); }
            out = bad != null
              ? {valid: false, errors: [{path: 'ops', code: 'duplicate_number', message: 'Number ' + bad + ' is duplicated.'}], effects: [], revision: doc.revision}
              : {valid: true, errors: [], effects: [], revision: doc.revision};
            break;
          }
          case 'GetChannelApplyResult': out = state.receipts[a.requestId] || {status: 'unknown'}; break;
          case 'GetChannelRefreshStatus': out = {libraryRevision: doc.revision, snapshotRevision: doc.revision,
            computedAt: null, channelId: a.channelId, pending: null, health: {healthStatus: 'ok', sceneCount: 12}}; break;
          case 'GetChannelHistory': out = {entries: [], revision: doc.revision}; break;
          default: out = {};
        }
        data = {runPluginOperation: out};
      } else if (body.query.includes('findTags')) {
        data = {findTags: {tags: [{id: '1', name: 'Tag 1', scene_count: 42}]}};
      } else if (body.query.includes('findTag(')) {
        data = {findTag: {name: 'Tag ' + vars.id}};
      }
      return route.fulfill({contentType: 'application/json', body: JSON.stringify({data})});
    }
    if (scripts[url.pathname]) {
      return route.fulfill({contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: scripts[url.pathname]});
    }
    return route.fulfill({contentType: 'text/html', body: html});
  });
  await page.goto('http://org.invalid/');
  await page.locator('.jw-dial-row').first().waitFor({timeout: 10000});
  return {context, page, state};
}

const failures = [];
function check(name, ok, detail) {
  console.log((ok ? '  ✓ ' : '  ✗ ') + name + (ok ? '' : (detail != null ? ' — ' + JSON.stringify(detail) : '')));
  if (!ok) failures.push(name);
  return ok;
}
const settle = (ms) => new Promise((r) => setTimeout(r, ms));
const dialNumbers = (page) => page.$$eval('.jw-dial-row .jw-dial-number', (els) => els.map((e) => e.textContent.trim()));
const dialNames = (page) => page.$$eval('.jw-dial-row .jw-dial-name', (els) => els.map((e) => e.textContent.trim()));

(async () => {
  const browser = await chromium.launch({executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome',
    headless: true, args: ['--no-sandbox']});

  // ================= A: capability gating (no features.arrangement) =========
  {
    const b = await boot(browser, {arrangement: false});
    const {page, state} = b;
    await settle(700);
    check('A: no Organize channels button', await page.getByRole('button', {name: 'Organize channels…'}).count() === 0);
    check('A: gate pill explains', (await page.locator('.jw-arrange-gate').count()) >= 1);
    check('A: basic editing boots (editor name field)', await page.locator('#f-name').count() === 1);
    check('A: no Move / insert link', await page.getByRole('button', {name: 'Move / insert…'}).count() === 0);
    check('A: no page errors', state.errors.length === 0, state.errors);
    await b.context.close();
  }

  // ================= B: create at occupied 300 -> resolution sheet =========
  {
    const b = await boot(browser);
    const {page, state} = b;
    await settle(500);
    await page.getByRole('button', {name: '+ New channel', exact: true}).first().click();
    await page.locator('#jw-new-name').fill('Conflict Channel');
    await page.locator('#jw-new-number').fill('300');
    await settle(200);
    check('B: occupied number explains the occupant',
      (await page.locator('.jw-dialog').getByText(/300 is “Taken A”/).count()) >= 1);
    await page.getByRole('button', {name: 'Create draft'}).click();
    await page.locator('.jw-sheet-numres').waitFor({timeout: 10000});
    await settle(700); // explain preview round-trip
    check('B: sheet opens with the choice matrix', await page.locator('.jw-choice').count() >= 4,
      await page.locator('.jw-choice').count());
    check('B: swap is absent for a new channel',
      await page.locator('.jw-choice', {hasText: 'Swap numbers'}).count() === 0);
    check('B: shift-up explains the run',
      (await page.locator('.jw-choice', {hasText: 'Insert and shift upward'}).textContent()).includes('stops at the first free number'));
    // default choice is insert-up; review shows the run 300,301,302 -> 301,302,303
    check('B: review shows displaced run', (await page.locator('.jw-sheet-numres .jw-review-row').count()) === 4,
      await page.locator('.jw-sheet-numres .jw-review-row').count());
    await page.getByRole('button', {name: 'Stage arrangement'}).click();
    await page.locator('.jw-arrange-bar').waitFor({timeout: 5000});
    check('B: arrangement bar appears with counts',
      (await page.locator('.jw-arrange-bar').textContent()).includes('renumbered'));
    await page.getByRole('button', {name: 'Apply arrangement'}).click();
    await page.locator('.jw-toast', {hasText: 'Arrangement applied at r13'}).waitFor({timeout: 15000});
    const nums = await dialNumbers(page);
    const names = await dialNames(page);
    const i300 = nums.indexOf('300');
    check('B: new channel aired at 300', i300 >= 0 && names[i300] === 'Conflict Channel', {nums, names});
    check('B: old run shifted (301..303 = A,B,C; D keeps 305)',
      names[nums.indexOf('301')] === 'Taken A' && names[nums.indexOf('302')] === 'Taken B'
      && names[nums.indexOf('303')] === 'Taken C' && names[nums.indexOf('305')] === 'Taken D');
    check('B: Stage reversal toast action present',
      await page.getByRole('button', {name: 'Stage reversal'}).count() >= 1);
    check('B: one revision, one packet',
      state.tasks.length === 1 && JSON.parse(state.tasks[0].ops).some((o) => o.op === 'channel.create')
      && JSON.parse(state.tasks[0].ops).some((o) => o.op === 'channels.renumber'));
    check('B: create skeleton merged the full draft (source rides)',
      (JSON.parse(state.tasks[0].ops).find((o) => o.op === 'channel.create') || {}).channel.source != null);
    check('B: no page errors', state.errors.length === 0, state.errors);
    await b.context.close();
  }

  // ================= C: block move with displaced review ===================
  {
    const b = await boot(browser);
    const {page, state} = b;
    await settle(500);
    // select the three block channels via Select channels mode
    await page.getByRole('button', {name: 'Select channels'}).first().click();
    for (const name of ['Block One', 'Block Two', 'Block Three']) {
      await page.locator('.jw-dial-row', {hasText: name}).click();
    }
    check('C: three selected', (await page.locator('.jw-bulk-bar strong').textContent()).startsWith('3 selected'));
    await page.getByRole('button', {name: 'Organize selection…'}).click();
    await page.locator('.jw-sheet-organize').waitFor({timeout: 5000});
    await page.getByRole('button', {name: 'Move block to start'}).click();
    await page.locator('#jw-org-bstart').fill('300');
    await settle(900); // preview debounce + round-trip
    await page.locator('.jw-review-list .jw-review-row').first().waitFor({timeout: 8000});
    const reviewText = await page.locator('.jw-sheet-organize').textContent();
    check('C: review lists displaced bystanders', reviewText.includes('displaced'), reviewText.slice(0, 400));
    check('C: block targets consecutive', reviewText.includes('171 → 300') && reviewText.includes('180 → 302'), reviewText.slice(0, 400));
    await page.getByRole('button', {name: 'Stage arrangement'}).click();
    await page.locator('.jw-arrange-bar').waitFor({timeout: 5000});
    await page.getByRole('button', {name: 'Apply arrangement'}).click();
    await page.locator('.jw-toast', {hasText: 'Arrangement applied at r13'}).waitFor({timeout: 15000});
    const nums = await dialNumbers(page);
    const names = await dialNames(page);
    check('C: block aired at 300..302 in order',
      names[nums.indexOf('300')] === 'Block One' && names[nums.indexOf('301')] === 'Block Two'
      && names[nums.indexOf('302')] === 'Block Three', {nums: nums.slice(0, 12), names: names.slice(0, 12)});
    check('C: displaced rows kept (Taken A..C still present above the block)',
      names.includes('Taken A') && names.includes('Taken C'));
    check('C: no page errors', state.errors.length === 0, state.errors);
    await b.context.close();
  }

  // ================= D: arrange range useAvailable (Performers 300-699) ====
  {
    const b = await boot(browser);
    const {page, state} = b;
    await settle(500);
    await page.locator('button[aria-label="Actions for group Performers"]').click({force: true});
    await page.getByRole('menuitem', {name: 'Arrange numbers…'}).click();
    await page.locator('.jw-sheet-organize').waitFor({timeout: 5000});
    await page.locator('#jw-org-rstart').fill('300');
    await page.locator('#jw-org-rend').fill('699');
    await settle(900);
    await page.locator('.jw-review-list .jw-review-row').first().waitFor({timeout: 8000});
    const text = await page.locator('.jw-sheet-organize').textContent();
    check('D: capacity line present', text.includes('Range 300–699 holds 400 slots'), text.slice(0, 500));
    check('D: outsiders keep their numbers (default)',
      text.includes('outsider') && text.includes('keep'), text.slice(0, 500));
    await page.getByRole('button', {name: 'Stage arrangement'}).click();
    await page.getByRole('button', {name: 'Apply arrangement'}).click();
    await page.locator('.jw-toast', {hasText: 'Arrangement applied at r13'}).waitFor({timeout: 15000});
    const nums = await dialNumbers(page);
    const names = await dialNames(page);
    check('D: outsiders untouched', names[nums.indexOf('320')] === 'Outsider One'
      && names[nums.indexOf('450')] === 'Outsider Two', {nums, names});
    check('D: performers packed inside 300–699',
      ['Perf One', 'Perf Two', 'Perf Three', 'Perf Four'].every((n) => {
        const i = names.indexOf(n);
        return i >= 0 && Number(nums[i]) >= 300 && Number(nums[i]) <= 699;
      }));
    check('D: no page errors', state.errors.length === 0, state.errors);
    await b.context.close();
  }

  // ================= E: exclusive range relocates outsiders ================
  {
    const b = await boot(browser);
    const {page, state} = b;
    await settle(500);
    await page.locator('button[aria-label="Actions for group Performers"]').click({force: true});
    await page.getByRole('menuitem', {name: 'Arrange numbers…'}).click();
    await page.locator('.jw-sheet-organize').waitFor({timeout: 5000});
    await page.locator('#jw-org-rstart').fill('300');
    await page.locator('#jw-org-rend').fill('699');
    await page.getByRole('button', {name: 'Make this range exclusive (one time)'}).click();
    await page.locator('#jw-org-ostart').fill('100');
    await page.locator('#jw-org-oend').fill('299');
    await settle(900);
    await page.locator('.jw-review-list .jw-review-row').first().waitFor({timeout: 8000});
    const text = await page.locator('.jw-sheet-organize').textContent();
    check('E: one-time-not-reservation copy', text.includes('one-time arrangement, not a permanent reservation'));
    check('E: outsiders must move called out', /outsider/.test(text) && /must move/.test(text), text.slice(0, 500));
    await page.getByRole('button', {name: 'Stage arrangement'}).click();
    await page.getByRole('button', {name: 'Apply arrangement'}).click();
    await page.locator('.jw-toast', {hasText: 'Arrangement applied at r13'}).waitFor({timeout: 15000});
    const nums = await dialNumbers(page);
    const names = await dialNames(page);
    const outIdx = nums.findIndex((n, i) => names[i] === 'Outsider One');
    check('E: outsiders relocated outside 300–699',
      outIdx >= 0 && (Number(nums[outIdx]) < 300 || Number(nums[outIdx]) > 699),
      {n: nums[outIdx]});
    check('E: no page errors', state.errors.length === 0, state.errors);
    await b.context.close();
  }

  // ================= F: undo / redo on the staged bar ======================
  {
    const b = await boot(browser);
    const {page, state} = b;
    await settle(500);
    await page.locator('button[aria-label="Actions for group Performers"]').click({force: true});
    await page.getByRole('menuitem', {name: 'Arrange numbers…'}).click();
    await page.locator('#jw-org-rstart').fill('300');
    await page.locator('#jw-org-rend').fill('699');
    await settle(900);
    await page.locator('.jw-review-list .jw-review-row').first().waitFor({timeout: 8000});
    await page.getByRole('button', {name: 'Stage arrangement'}).click();
    await page.locator('.jw-arrange-bar').waitFor({timeout: 5000});
    check('F: staged overlay renumbers the dial preview',
      (await page.locator('.jw-dial-number-staged').count()) >= 1,
      await page.locator('.jw-dial-number-staged').count());
    await page.getByRole('button', {name: 'Undo'}).click();
    await settle(300);
    check('F: undo clears the staged bar', await page.locator('.jw-arrange-bar').count() === 0);
    check('F: undo clears the dial overlay', await page.locator('.jw-dial-number-staged').count() === 0);
    await page.locator('.jw-arrange-bar').waitFor({timeout: 100}).catch(() => {});
    await page.locator('button[aria-label="Actions for group Performers"]').click({force: true});
    await page.getByRole('menuitem', {name: 'Arrange numbers…'}).click();
    await page.locator('#jw-org-rstart').fill('300');
    await page.locator('#jw-org-rend').fill('699');
    await settle(900);
    await page.locator('.jw-review-list .jw-review-row').first().waitFor({timeout: 8000});
    await page.getByRole('button', {name: 'Stage arrangement'}).click();
    await page.locator('.jw-arrange-bar').waitFor({timeout: 5000});
    check('F: re-stage works after undo', await page.locator('.jw-arrange-bar').count() === 1);
    check('F: no page errors', state.errors.length === 0, state.errors);
    await b.context.close();
  }

  // ================= G: revision conflict -> Reload and replan =============
  {
    const b = await boot(browser);
    const {page, state} = b;
    await settle(500);
    await page.locator('button[aria-label="Actions for group Performers"]').click({force: true});
    await page.getByRole('menuitem', {name: 'Arrange numbers…'}).click();
    await page.locator('#jw-org-rstart').fill('300');
    await page.locator('#jw-org-rend').fill('699');
    await settle(900);
    await page.locator('.jw-review-list .jw-review-row').first().waitFor({timeout: 8000});
    await page.getByRole('button', {name: 'Stage arrangement'}).click();
    await page.locator('.jw-arrange-bar').waitFor({timeout: 5000});
    // another writer commits between preview and Apply
    doc.revision = 13;
    await page.getByRole('button', {name: 'Apply arrangement'}).click();
    await settle(2500);
    const barText = await page.locator('.jw-arrange-bar').textContent();
    check('G: bar reports the stale base', barText.includes('Based on r12') && barText.includes('now r13'), barText);
    check('G: Reload and replan offered', await page.getByRole('button', {name: 'Reload and replan'}).count() === 1);
    check('G: conflict toast keeps the draft', (await page.locator('.jw-toast', {hasText: 'intact'}).count()) >= 1
      || (await page.locator('.jw-toast', {hasText: 'Revision conflict'}).count()) >= 1,
      await page.locator('.jw-toast-zone').textContent());
    check('G: nothing committed', doc.channels.some((c) => c.number === 400 && c.name === 'Perf One'));
    check('G: no page errors', state.errors.length === 0, state.errors);
    await b.context.close();
  }

  console.log('');
  if (failures.length) {
    console.log('FAILURES (' + failures.length + '): ' + failures.join(' | '));
    process.exit(1);
  }
  console.log('ALL FLOWS GREEN');
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
