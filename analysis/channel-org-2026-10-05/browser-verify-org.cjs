// Browser verification harness — Phase 4d, channel-organization feature.
// Serves the REAL ui/index.js + ui/styles.css in headless Chromium against a
// mock GraphQL transport whose org surface is a RECORDING PROXY to the real
// Python layer: every PreviewChannelArrangement / ApplyChannelChanges /
// ValidateChannelChanges / GetChannelApplyResult call shells out to
// py_driver.py (justwatch.organization + justwatch.library +
// justwatch.channel_ops) against a per-context seeded temp data dir. The
// browser flows therefore run against the REAL planner/validator/transaction
// code — no JS re-implementation. Pattern adapted from
// analysis/ui-polish-2026-09-28/browser-verify.cjs.
//
//   node browser-verify-org.cjs [outDir]        # full dry run
//   FLOW=3 node browser-verify-org.cjs          # one flow (debug)
//
// REMEDIATION GATE (R12, 2026-10-06): the process exits NONZERO when ANY
// behavioral assertion fails — failKnown AND failNew — or when any flow
// crashes. There are no expected failures on a fixed tree: the remediation
// converted every F1/F3/F4/F5/F6 known-failure marker into a passing
// assertion. failKnown reporting is kept so a future expected-failure can
// still be distinguished from a new finding in the JSON, but it can NEVER
// satisfy the gate.
const fs = require('fs');
const path = require('path');
const {execFile} = require('child_process');
const {chromium} = require(process.env.JW_AUDIT_PLAYWRIGHT || '/tmp/stash-verify/node_modules/playwright-core');

const ROOT = path.resolve(__dirname, '../..');
const DEPS = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';
const SHOTS = process.argv[2] || path.join(__dirname, 'shots');
const DRIVER = path.join(__dirname, 'py_driver.py');
const EXTRAS_SRC = fs.readFileSync(path.join(ROOT, 'extras/channel-studio-custom.js'), 'utf8');
const {LIBRARY_ID, REVISION, buildFixture, ENTITIES} = require('./org-fixtures.js');

fs.rmSync(SHOTS, {recursive: true, force: true});
fs.mkdirSync(path.join(SHOTS, '.tmp'), {recursive: true});
const log = (line) => { console.log(line); fs.appendFileSync(path.join(SHOTS, 'dry-run.log'), line + '\n'); };

// ---------------------------------------------------------------- report ----
const report = {flowStatus: {}, failKnown: {}, failNew: [], failHarness: [], notes: [], shots: []};
report.notes.push('remediation rerun (2026-10-06): expectations updated to the FIXED tree — the 2026-10-05 review\'s F1/F3/F4/F5/F6 known-failure markers and the empty-create-source/review-wrap notes are now passing assertions (R1–R11); flows 12–21 are the new R1–R10 real-bundle regressions. The gate exits nonzero on ANY behavioral failure or crash (R12).');
function check(name, ok, detail) {
  log('    ' + (ok ? 'ok   ' : 'FAIL ') + name + (ok || detail == null ? '' : ' — ' + JSON.stringify(detail).slice(0, 300)));
  if (ok) return true;
  report.failNew.push({name, detail: detail == null ? null : JSON.stringify(detail).slice(0, 500)});
  return false;
}
let currentFlow = null;
function checkKnown(name, ok, fix, detail) {
  // Mechanism kept for known-vs-new FAILURE REPORTING only. Post-remediation
  // there are no expected failures: a checkKnown failure fails the gate
  // exactly like a new one (R12).
  if (ok) { log('    ok   ' + name); return true; }
  log('    known(' + fix + ') ' + name + ' — ' + JSON.stringify(detail).slice(0, 300));
  (report.failKnown[fix] = report.failKnown[fix] || []).push({flow: currentFlow, name, detail: JSON.stringify(detail).slice(0, 500)});
  return false;
}
const settle = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- driver ----
// Requests travel as a temp FILE + argv: piping stdin into the child
// deadlocks in this sandbox (execFile {input} never completes).
function driverRaw(dataDir, cmd, args) {
  return new Promise((resolve, reject) => {
    const reqFile = path.join(dataDir, '.req-' + Math.random().toString(36).slice(2) + '.json');
    fs.writeFileSync(reqFile, JSON.stringify(args || {}));
    execFile('python3', [DRIVER, cmd, dataDir, reqFile], {maxBuffer: 32 * 1024 * 1024, timeout: 45000},
      (err, stdout) => {
        fs.unlink(reqFile, () => {});
        let parsed = null;
        try { parsed = JSON.parse(stdout); } catch (e) { /* fallthrough */ }
        if (err && !parsed) return reject(new Error('driver ' + cmd + ' crashed: ' + (err.message || err)));
        if (!parsed) return reject(new Error('driver ' + cmd + ' unparseable output: ' + String(stdout).slice(0, 200)));
        if (!parsed.ok) return reject(new Error(parsed.error));
        resolve(parsed.result);
      });
  });
}

// ------------------------------------------------------------ page boot ----
const FAS = {
  faTags: {iconName: 'tags', icon: [512, 512, [], 'f02c']},
  faUser: {iconName: 'user', icon: [448, 512, [], 'f007']},
  faBuilding: {iconName: 'building', icon: [384, 512, [], 'f1ad']},
  faTrashAlt: {iconName: 'trash-alt', icon: [448, 512, [], 'f2ed']},
  faTv: {iconName: 'tv', icon: [640, 512, [], 'f26c']},
};

function makePage(theme, extrasTag) {
  const scripts = {
    '/react.js': fs.readFileSync(path.join(DEPS, 'react/umd/react.development.js')),
    '/react-dom.js': fs.readFileSync(path.join(DEPS, 'react-dom/umd/react-dom.development.js')),
    '/ui.js': fs.readFileSync(path.join(ROOT, 'ui/index.js')),
    '/ui.css': fs.readFileSync(path.join(ROOT, 'ui/styles.css')),
  };
  const vars = theme === 'light'
    ? '--body-color:#f8f9fa;--text-color:#212529;--primary:#0d6efd;--danger:#dc3545;'
    : '--body-color:#202b33;--text-color:#f0f0f5;--primary:#6caddf;--danger:#d9534f;';
  const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/ui.css">
<style>:root{${vars}}
/* Stash ships bootstrap reboot (border-box everywhere); the harness page
   mirrors that so geometry assertions match production. */
*, *::before, *::after { box-sizing: border-box; }
</style>
<body style="background:var(--body-color);margin:0"><div id="root"></div>
<script src="/react.js"></script><script src="/react-dom.js"></script>
<script>
window.PluginApi = {React, libraries:{FontAwesomeSolid: ${JSON.stringify(FAS)}},
  components:{Icon: function Icon(p){ return React.createElement('span', {className:p.className, style:p.style,
    'data-icon': (p.icon && p.icon.iconName) || ''}, (p.icon && p.icon.iconName) || ''); }},
  register:{route(p,c){window.AuditApp=c}}};
</script>
<script src="/ui.js"></script>
<script>ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(window.AuditApp));</script>
${extrasTag || ''}`;
  return {scripts, html};
}

let ctxSeq = 0;
async function boot(browser, opts) {
  const o = opts || {};
  const dataDir = path.join(SHOTS, '.tmp', 'data-' + Date.now() + '-' + (++ctxSeq));
  fs.mkdirSync(dataDir, {recursive: true});
  await driverRaw(dataDir, 'seed', buildFixture());
  const state = {
    dataDir, errors: [], console: [], previews: [], applies: [],
    conflictBump: 0, abortNextTask: 0, abortAfterTask: 0, holdApplyMs: null,
    bumpSeq: 0, previewDelays: [],
    arrangement: o.arrangement !== false,
  };
  const drv = (cmd, args) => driverRaw(dataDir, cmd, args);

  const {scripts, html} = makePage(o.theme || 'dark');
  const context = await browser.newContext({
    viewport: o.viewport || {width: 1500, height: 950},
    deviceScaleFactor: o.dsf || 1,
  });
  const page = await context.newPage();
  page.on('pageerror', (e) => state.errors.push(e.message));
  page.on('console', (m) => state.console.push(m.text()));

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/graphql') {
      const body = route.request().postDataJSON();
      const vars = body.variables || {};
      if (body.query.includes('runPluginTask') && state.abortNextTask > 0) {
        state.abortNextTask--;
        return route.abort('connectionreset'); // lost-response simulation
      }
      try {
        const data = await handleGql(state, drv, body, vars);
        return route.fulfill({contentType: 'application/json', body: JSON.stringify({data})});
      } catch (e) {
        return route.fulfill({contentType: 'application/json',
          body: JSON.stringify({errors: [{message: String((e && e.message) || e)}]})});
      }
    }
    if (scripts[url.pathname]) {
      return route.fulfill({contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript',
        body: scripts[url.pathname]});
    }
    return route.fulfill({contentType: 'text/html', body: html});
  });

  await page.goto('http://org.invalid/');
  await page.locator('.jw-dial-row').first().waitFor({timeout: 30000});
  await settle(500);

  const api = {
    page, context, state, drv,
    doc: async () => drv('get_library', {limit: 1000, offset: 0}),
    number: async (name) => {
      const doc = await api.doc();
      const hit = doc.channels.find((c) => c.name === name);
      return hit ? hit.number : null;
    },
    row: (name) => api.page.locator('.jw-dial-row', {hasText: name}).first(),
    shot: async (name) => {
      const file = path.join(SHOTS, name + '.png');
      await api.page.screenshot({path: file});
      report.shots.push(name);
      log('    shot ' + name);
    },
    rev: async () => api.page.locator('.jw-revision-chip').textContent().then((t) => parseInt(t.replace('r', ''), 10)),
    session: (key) => api.page.evaluate((k) => {
      try { return JSON.parse(sessionStorage.getItem(k)); } catch (e) { return null; }
    }, key),
    drafts: () => api.session('jw-studio-drafts-v1:' + LIBRARY_ID),
    org: () => api.session('jw-studio-org-v1:' + LIBRARY_ID),
    armConflictBump: () => { state.conflictBump++; },
    armAbort: () => { state.abortNextTask++; },
    armAbortAfter: () => { state.abortAfterTask++; },
    armHoldApply: (ms) => { state.holdApplyMs = ms; },
    armPreviewDelay: (match, ms, once) => { state.previewDelays.push({match, ms, once: once !== false}); },
  };
  return api;
}

async function handleGql(state, drv, body, vars) {
  if (body.query.includes('runPluginTask')) {
    const a = vars.a;
    if (state.holdApplyMs) { await settle(state.holdApplyMs); state.holdApplyMs = null; }
    if (state.conflictBump > 0) {
      state.conflictBump--;
      const cur = await drv('get_library', {});
      await drv('apply', {
        requestId: 'harness-other-writer-' + (++state.bumpSeq),
        expectedRevision: String(cur.revision),
        ops: JSON.stringify([{op: 'channels.patch', channelIds: ['net_f0000001'], patch: {paused: true}}]),
        actor: 'harness-other-writer',
      });
    }
    const receipt = await drv('apply', {
      requestId: a.requestId, expectedRevision: a.expectedRevision, ops: a.ops, actor: 'channel-studio',
    });
    state.applies.push({requestId: a.requestId, expected: a.expectedRevision, ops: JSON.parse(a.ops), receipt});
    if (state.abortAfterTask > 0) {
      state.abortAfterTask--;
      return route.abort('connectionreset'); // R6b: committed, response lost
    }
    return {runPluginTask: 'job-' + state.applies.length};
  }
  if (body.query.includes('runPluginOperation')) {
    const a = vars.args || {};
    switch (a.mode) {
      case 'Capabilities': return {runPluginOperation: await drv('capabilities', {arrangement: state.arrangement})};
      case 'GetChannelLibrary': return {runPluginOperation: await drv('get_library', a)};
      case 'GetChannelDefinition': return {runPluginOperation: await drv('get_definition', a)};
      case 'ValidateChannelChanges': return {runPluginOperation: await drv('validate', a)};
      case 'PreviewChannelArrangement': {
        // R1 support: hold a matching response so an older/newer preview can
        // land out of order. Rules are {match(intentArgs), ms, once?}.
        for (let i = 0; i < state.previewDelays.length; i++) {
          const d = state.previewDelays[i];
          let hit = false;
          try { hit = !!d.match({intent: JSON.parse(a.intent), args: a}); } catch (e) { hit = false; }
          if (hit) {
            if (d.once) state.previewDelays.splice(i, 1);
            await settle(d.ms);
            break;
          }
        }
        const out = await drv('preview', a);
        let groups = [];
        try { groups = a.groups ? JSON.parse(a.groups) : []; } catch (e) { groups = []; }
        state.previews.push({intent: JSON.parse(a.intent), overlays: a.channels ? JSON.parse(a.channels) : [],
          groups, expectedRevision: a.expectedRevision,
          valid: out.valid, noop: out.noop, errors: out.errors});
        return {runPluginOperation: out};
      }
      case 'PreviewChannelPool': return {runPluginOperation: await drv('pool', a)};
      case 'GetChannelApplyResult': return {runPluginOperation: await drv('receipt', a)};
      case 'GetChannelHistory': return {runPluginOperation: await drv('history', a)};
      case 'GetChannelRefreshStatus': {
        try { return {runPluginOperation: await drv('refresh_status', a)}; }
        catch (e) { return {runPluginOperation: {libraryRevision: null, pending: null, health: null}}; }
      }
      case 'RequeueChannelRefresh': return {runPluginOperation: {queued: true}};
      default: return {runPluginOperation: {}};
    }
  }
  // Stash entity lookups (picker + name resolution)
  const q = (vars.f && vars.f.q) || '';
  if (body.query.includes('findTags')) {
    return {findTags: {tags: ENTITIES.tags.filter((t) => !q || t.name.toLowerCase().includes(q))
      .map((t) => Object.assign({scene_count: 5}, t))}};
  }
  if (body.query.includes('findPerformers')) {
    return {findPerformers: {performers: ENTITIES.performers.filter((t) => !q || t.name.toLowerCase().includes(q))
      .map((t) => Object.assign({scene_count: 5}, t))}};
  }
  if (body.query.includes('findStudios')) {
    return {findStudios: {studios: ENTITIES.studios.filter((t) => !q || t.name.toLowerCase().includes(q))
      .slice(0, 100).map((t) => Object.assign({scene_count: 5}, t))}};
  }
  if (body.query.includes('findTag(')) return {findTag: {name: (ENTITIES.tags.find((t) => t.id === vars.id) || {}).name || null}};
  if (body.query.includes('findPerformer(')) return {findPerformer: {name: 'Performer ' + vars.id}};
  if (body.query.includes('findStudio(')) return {findStudio: {name: 'Studio ' + vars.id}};
  return {};
}

// ------------------------------------------------------------ selectors ----
const waitReview = async (api, minRows = 1) => {
  await api.page.locator('.jw-review-row').nth(minRows - 1).waitFor({timeout: 20000});
  await settle(300);
};
const waitChoices = async (api) => {
  await api.page.locator('.jw-choice').first().waitFor({timeout: 20000});
  await settle(250);
};
// The Stage button is each sheet footer's ONLY .jw-btn-primary. Its label is
// dynamic since UX2 ("Stage N channel moves" / "Stage arrangement" /
// "Planning…"), so locate by class, not by name.
const stageButton = (api, sheetClass) =>
  api.page.locator('.' + sheetClass + ' button.jw-btn-primary').first();
async function stageFromSheet(api, sheetClass) {
  await stageButton(api, sheetClass).click();
  await api.page.locator('.jw-arrange-bar').waitFor({timeout: 10000});
}
const arrangeBar = (api) => api.page.locator('.jw-arrange-bar');
async function applyArrangement(api, revAfter) {
  await arrangeBar(api).getByRole('button', {name: /^Apply/}).click();
  try {
    await api.page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
  } catch (e) {
    const receipts = api.state.applies.map((a) => ({requestId: a.requestId.slice(0, 12), status: a.receipt.status,
      error: a.receipt.error, errors: (a.receipt.errors || []).map((x) => x.code + ':' + x.message).slice(0, 3)}));
    throw new Error('applyArrangement: no applied toast; receipts=' + JSON.stringify(receipts) +
      ' toasts=' + JSON.stringify(await api.page.locator('.jw-toast').allTextContents()));
  }
  await settle(400);
  if (revAfter != null) {
    // the committed revision reaches the chip via refreshLibrary — poll,
    // never a single read (R5c's bulk path was flaky here).
    let seen = await api.rev();
    for (let i = 0; i < 20 && seen !== revAfter; i++) { await settle(300); seen = await api.rev(); }
    check('revision advanced to ' + revAfter, seen === revAfter, seen);
  }
}
// In-flight marker since R6: a status pill ("Submitting…"/"Applying…"), NOT
// a disabled Apply button — the button leaves the DOM while a request runs.
const applyingPill = (api) => api.page.locator('.jw-arrange-bar .jw-pill-applying');
// R6 unknown-outcome state: the bar must offer Check result / Retry same
// Apply and never stay stuck on a phantom "Applying…".
async function waitUnknownOutcome(api) {
  await api.page.locator('.jw-arrange-stale', {hasText: 'outcome is unknown'}).waitFor({timeout: 15000});
  await api.page.locator('.jw-arrange-bar').getByRole('button', {name: 'Check result'}).waitFor({timeout: 5000});
  await api.page.locator('.jw-arrange-bar').getByRole('button', {name: 'Retry same Apply'}).waitFor({timeout: 5000});
  await settle(200);
}
// The editor's rules facet: pick one tag so a new channel's source passes the
// server's stored-shape validation (empty criteria is a typed error).
async function authorTagRule(api, tag) {
  const {page} = api;
  await page.locator('.jw-rule-row', {hasText: 'Tags'}).first()
    .getByRole('button', {name: /Choose \(/}).click();
  await page.locator('.jw-pick-row', {hasText: tag}).first().waitFor({timeout: 15000});
  await page.locator('.jw-pick-row', {hasText: tag}).first().click();
  await page.getByRole('button', {name: 'Done', exact: true}).click();
  await settle(300);
}
async function openNewChannel(api, {number, name, createGroup} = {}) {
  await api.page.locator('.jw-new-channel').click();
  await api.page.locator('#jw-new-name').waitFor();
  if (number != null) await api.page.locator('#jw-new-number').fill(String(number));
  if (name != null) await api.page.locator('#jw-new-name').fill(name);
  if (createGroup) {
    await api.page.locator('#jw-new-group').selectOption('__create__');
    await api.page.locator('input[aria-label="New group name"]').last().fill(createGroup);
  }
}
async function ensureBulkMode(api) {
  if (await api.page.locator('.jw-bulk-bar').count() === 0) {
    await api.page.getByRole('button', {name: 'Select channels', exact: true}).click();
  }
}
async function selectRows(api, names) {
  for (const n of names) await api.row(n).click();
}
// Navigate the editor to a channel and wait until ITS editor is mounted (the
// remount is async — filling #f-name earlier can hit the previous editor).
async function openEditor(api, name) {
  if (await api.page.locator('.jw-bulk-bar').count() > 0) {
    await api.page.getByRole('button', {name: 'Done selecting'}).click();
    await settle(250);
  }
  await api.row(name).click();
  try {
    await api.page.locator('.jw-editor-head-name', {hasText: name}).waitFor({timeout: 15000});
  } catch (e) {
    await api.shot('debug-openeditor-' + name.replace(/\W/g, ''));
    const heads = await api.page.locator('.jw-editor-head-name').allTextContents();
    const sel = await api.page.evaluate(() =>
      [...document.querySelectorAll('.jw-dial-row[aria-selected="true"]')].map((n) => n.getAttribute('aria-label')));
    const overlays = await api.page.locator('.jw-overlay').count();
    throw new Error('openEditor(' + name + ') failed: heads=' + JSON.stringify(heads) +
      ' selected=' + JSON.stringify(sel) + ' overlays=' + overlays + ' pageErrors=' + JSON.stringify(api.state.errors));
  }
  await settle(250);
}
// The group-header "⋯" is visibility:hidden until hover/focus — hover first.
async function openGroupMenu(api, group) {
  const header = api.page.locator('.jw-group-header', {hasText: group}).first();
  await header.hover();
  await settle(200);
  await header.locator('button[aria-label="Actions for group ' + group + '"]').click();
}

// ---------------------------------------------------------------- flows ----

// 1. Create at occupied 300 → sheet (no Swap), shift-up chain, one Apply;
//    downward mirror.
async function flow1(browser) {
  const api = await boot(browser);
  const {page} = api;
  await openNewChannel(api, {number: 300, name: 'Fresh Cut'});
  await check('occupied hint offers placement', await page.locator('.jw-hint', {hasText: 'Occupied 300'}).count() === 1);
  await page.getByRole('button', {name: 'Create draft'}).click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  check('sheet title names the subject at 300',
    (await page.locator('.jw-dialog-title').textContent()).includes('Fresh Cut') &&
    (await page.locator('.jw-dialog-title').textContent()).includes('300'));
  check('no Swap choice for a new channel', await page.locator('.jw-choice', {hasText: 'Swap numbers'}).count() === 0);
  check('shift-up preselected with the real chain (3 move, stops at 303)',
    await page.locator('.jw-choice', {hasText: 'Insert and shift upward'}).locator('input').isChecked() &&
    (await page.locator('.jw-choice', {hasText: 'Insert and shift upward'}).textContent()).includes('3 existing channels move') &&
    (await page.locator('.jw-choice', {hasText: 'Insert and shift upward'}).textContent()).includes('303'));
  await waitReview(api, 4);
  const rowTexts = await page.locator('.jw-review-row').allTextContents();
  check('review lists the 300→303 chain + the new row',
    rowTexts.filter((t) => t.includes('Occupied')).length === 3 &&
    rowTexts.some((t) => t.includes('Occupied 300') && t.includes('300 → 301')) &&
    rowTexts.some((t) => t.includes('Occupied 302') && t.includes('shifted up')) &&
    rowTexts.some((t) => t.includes('Fresh Cut')), rowTexts);
  // a brand-new draft has no rules yet: the sheet cannot make the merged
  // create commit — author one rule first (the honest owner path), then place.
  await page.getByRole('button', {name: 'Cancel — keep drafts'}).click();
  await settle(400);
  await authorTagRule(api, 'Tag 1');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  await waitReview(api, 4);
  await stageFromSheet(api, 'jw-sheet-numres');
  check('staged bar names the placement',
    (await page.locator('.jw-arrange-bar').textContent()).includes('1 new channel placed'));
  await applyArrangement(api, REVISION + 1);
  check('exactly ONE apply for the whole chain', api.state.applies.length === 1, api.state.applies.length);
  check('dial renumbered: Fresh Cut at 300', await api.number('Fresh Cut') === 300, await api.number('Fresh Cut'));
  check('Occupied 302 pushed to 303', await api.number('Occupied 302') === 303, await api.number('Occupied 302'));
  const createdLight = (await api.doc()).channels.find((c) => c.name === 'Fresh Cut');
  const created = createdLight
    ? (await api.drv('get_definition', {channelId: createdLight.id})).channel : null;
  check('created channel carries the authored rule + provenance', created && created.source.tagsAny &&
    created.source.tagsAny.length === 1 && created.provenance.origin === 'created', created && created.source);

  // downward mirror
  await openNewChannel(api, {number: 301, name: 'Deep Cut'});
  await page.getByRole('button', {name: 'Create draft'}).click();
  await page.locator('.jw-sheet-numres').waitFor();
  await settle(400);
  await page.getByRole('button', {name: 'Cancel — keep drafts'}).click();
  await settle(400);
  await authorTagRule(api, 'Tag 2');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  await page.locator('.jw-choice', {hasText: 'Insert and shift downward'}).click();
  await waitReview(api, 6);
  check('downward chain shifts 5 (stops at 296)',
    (await page.locator('.jw-choice', {hasText: 'Insert and shift downward'}).textContent()).includes('5 existing channels move'));
  await stageFromSheet(api, 'jw-sheet-numres');
  await applyArrangement(api, REVISION + 2);
  check('Deep Cut at 301 / Fresh Cut pushed to 299', await api.number('Deep Cut') === 301 && await api.number('Fresh Cut') === 299,
    {deep: await api.number('Deep Cut'), fresh: await api.number('Fresh Cut')});
  check('still one apply per arrangement', api.state.applies.length === 2, api.state.applies.length);
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 2. Existing 299 → occupied 300 with a dirty name: opt-in put + renumber in
//    ONE packet.
async function flow2(browser) {
  const api = await boot(browser);
  const {page} = api;
  await openEditor(api, 'Chart Three');
  await page.locator('#f-name').fill('Chart Three Renamed');
  check('editor shows the unsaved draft', (await page.locator('.jw-apply-state').textContent()).includes('Unsaved draft'));
  // contrast: an EXISTING channel sees Swap in the sheet
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  check('existing row gets a Swap choice', await page.locator('.jw-choice', {hasText: 'Swap numbers'}).count() === 1);
  await page.keyboard.press('Escape');
  await settle(200);

  await ensureBulkMode(api);
  await selectRows(api, ['Chart Three']);
  await page.getByRole('button', {name: 'Organize selection…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  check('selection scope active (1)', await page.locator('.jw-chip-active', {hasText: 'Current selection (1)'}).count() === 1);
  await page.getByRole('button', {name: 'Move block to start'}).click();
  await page.locator('#jw-org-bstart').fill('300');
  await waitReview(api, 2);
  check('displaced occupant listed in review',
    (await page.locator('.jw-review-row', {hasText: 'Occupied 300'}).textContent()).includes('displaced') &&
    (await page.locator('.jw-review-row', {hasText: 'Occupied 300'}).textContent()).includes('300 → 303'));
  const meta = await page.locator('.jw-review-tools .jw-hint').textContent();
  check('review meta counts 1 displaced bystander', meta.includes('1 selected channel') && meta.includes('1 other moves'), meta);
  await page.locator('.jw-review-row', {hasText: 'Chart Three'}).getByRole('button', {name: 'include draft…'}).click();
  check('draft opt-in latches', await page.locator('.jw-review-row', {hasText: 'Chart Three'}).getByRole('button', {name: 'draft included ✓'}).count() === 1);
  await stageFromSheet(api, 'jw-sheet-organize');
  const bar = await page.locator('.jw-arrange-bar').textContent();
  check('bar summarizes 2 renumbered + 1 relocated', bar.includes('2 channels renumbered') && bar.includes('1 other channel relocated'), bar);
  await applyArrangement(api, REVISION + 1);
  check('ONE packet committed it', api.state.applies.length === 1, api.state.applies.length);
  const doc = await api.doc();
  const moved = doc.channels.find((c) => c.id === 'net_a0000003');
  const occ = doc.channels.find((c) => c.id === 'net_b0000001');
  check('renumber + dirty name committed together (299→300)', moved.number === 300 && moved.name === 'Chart Three Renamed',
    {number: moved.number, name: moved.name});
  check('displaced occupant landed at 303', occ.number === 303, occ.number);
  const put = api.state.applies[0].ops.find((op) => op.op === 'channel.put');
  check('wire put carries the final number (agreement rule)', put && put.channel.number === 300 && put.channel.name === 'Chart Three Renamed', put && put.channel);
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 3. Block 171/173/180 → 300 with bystanders: displaced rows reviewed;
//    committed consecutively.
async function flow3(browser) {
  const api = await boot(browser);
  const {page} = api;
  await ensureBulkMode(api);
  await selectRows(api, ['Blocker 171', 'Blocker 173', 'Blocker 180']);
  await page.getByRole('button', {name: 'Organize selection…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'Move block to start'}).click();
  await page.locator('#jw-org-bstart').fill('300');
  await waitReview(api, 6);
  const meta = await page.locator('.jw-review-tools .jw-hint').textContent();
  check('review lists all 6 rows / 3 displaced bystanders', meta.includes('3 selected channels move') && meta.includes('3 others move'), meta);
  for (const occ of ['Occupied 300', 'Occupied 301', 'Occupied 302']) {
    check('displaced row reviewed: ' + occ,
      (await page.locator('.jw-review-row', {hasText: occ}).textContent()).includes('pushed up by the incoming block'));
  }
  await stageFromSheet(api, 'jw-sheet-organize');
  await applyArrangement(api, REVISION + 1);
  check('one apply committed the whole block', api.state.applies.length === 1, api.state.applies.length);
  const doc = await api.doc();
  const num = (id) => doc.channels.find((c) => c.id === id).number;
  check('block landed consecutively at 300-302',
    num('net_c0000001') === 300 && num('net_c0000002') === 301 && num('net_c0000004') === 302,
    {b171: num('net_c0000001'), b173: num('net_c0000002'), b180: num('net_c0000004')});
  check('displaced trio pushed consecutively to 303-305',
    num('net_b0000001') === 303 && num('net_b0000002') === 304 && num('net_b0000003') === 305,
    {o300: num('net_b0000001'), o301: num('net_b0000002'), o302: num('net_b0000003')});
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 4. Performers group → 300–699 useAvailable (outsiders stay, capacity line,
//    F6 alpha probe); shortfall case refuses staging with the exact number.
async function flow4(browser) {
  const api = await boot(browser);
  const {page} = api;
  await openGroupMenu(api, 'Performers');
  await page.getByRole('menuitem', {name: 'Arrange numbers…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  check('group scope preselected (2 channels)',
    (await page.locator('.jw-task-scope-line').first().textContent()).includes('Performers') &&
    (await page.locator('.jw-task-scope-line').first().textContent()).includes('2 channel'),
    await page.locator('.jw-task-scope-line').first().textContent().catch(() => null));
  await page.locator('#jw-org-rstart').fill('300');
  await page.locator('#jw-org-rend').fill('699');
  await waitReview(api, 2);
  const cap = await page.locator('.jw-scope-line', {hasText: 'holds 400 slots'}).textContent();
  check('capacity line exact', cap.includes('holds 400 slots') && cap.includes('selection needs 2') &&
    cap.includes('5 outsiders keep their numbers'), cap);
  check('outsiders 320/450 untouched in review', await page.locator('.jw-review-row', {hasText: 'Outsider'}).count() === 0);
  // R7 (was known-fail F6): the Alphabetical chip now sends order:"name" —
  // the planner must accept it and plan by name (Alpha Net first).
  await page.getByRole('button', {name: 'Alphabetical (A–Z, ties by channel id)'}).click();
  await settle(1600);
  const alphaRows = await page.locator('.jw-review-row').count();
  let alphaOk = false;
  let alphaDetail = null;
  if (alphaRows > 0) {
    alphaOk = (await page.locator('.jw-review-row').first().textContent()).includes('Alpha Net');
  } else {
    alphaDetail = await page.locator('.jw-review-errors').textContent().catch(() => null);
  }
  check('R7: alphabetical arrange_range plans by name (Alpha Net first)',
    alphaOk, alphaDetail || (alphaRows + ' review rows'));
  check('R7: frontend sends the canonical order enum ("name", never "alpha")',
    api.state.previews.length > 0 &&
    api.state.previews[api.state.previews.length - 1].intent.order === 'name',
    api.state.previews[api.state.previews.length - 1] || 'no previews');
  await page.getByRole('button', {name: 'Keep current number order'}).click();
  await waitReview(api, 2);
  await stageFromSheet(api, 'jw-sheet-organize');
  await applyArrangement(api, REVISION + 1);
  check('number-order pack: Zulu 310→303, Alpha 311→304; outsiders stay',
    await api.number('Zulu Net') === 303 && await api.number('Alpha Net') === 304 &&
    await api.number('Outsider 320') === 320 && await api.number('Outsider 450') === 450,
    {zulu: await api.number('Zulu Net'), alpha: await api.number('Alpha Net')});

  // shortfall: Trio (3) into 300-302 (fully occupied)
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'An entire group'}).click();
  await page.locator('#jw-org-group').selectOption('grp_trio001');
  await page.locator('#jw-org-rstart').fill('300');
  await page.locator('#jw-org-rend').fill('302');
  await page.locator('.jw-scope-line', {hasText: 'SHORT BY 3'}).waitFor({timeout: 20000});
  const shortLine = await page.locator('.jw-scope-line', {hasText: 'SHORT BY 3'}).textContent();
  check('shortfall line carries the exact number', shortLine.includes('SHORT BY 3') && shortLine.includes('holds 3 slots'), shortLine);
  check('staging refused while short', await stageButton(api, 'jw-sheet-organize').isDisabled());
  await page.getByRole('button', {name: 'Close — keep staging'}).click();
  check('nothing staged by the refused plan',
    (await page.locator('.jw-arrange-bar:not(.jw-arrange-bar-empty)').count()) === 0);
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 5. Exclusive arrangement with an explicit outside interval: outsiders
//    relocated; "one-time, not a reservation" copy.
async function flow5(browser) {
  const api = await boot(browser);
  const {page} = api;
  await ensureBulkMode(api);
  await selectRows(api, ['Blocker 171', 'Blocker 173', 'Blocker 180']);
  await page.getByRole('button', {name: 'Organize selection…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'Arrange within range'}).click();
  await page.locator('#jw-org-rstart').fill('171');
  await page.locator('#jw-org-rend').fill('180');
  await waitReview(api, 1);
  // UX3: the exclusive strategy is a plain-language chip pair now.
  await page.getByRole('button', {name: 'Move other channels out of this range'}).click();
  check('one-time copy present', await page.locator('.jw-hint', {hasText: 'one-time arrangement'}).count() === 1);
  await page.locator('#jw-org-ostart').fill('200');
  await page.locator('#jw-org-oend').fill('210');
  await waitReview(api, 3);
  const cap = await page.locator('.jw-scope-line', {hasText: 'holds 10 slots'}).textContent();
  check('exclusive capacity line', cap.includes('selection needs 3') && cap.includes('1 outsider must move') &&
    cap.includes('11 free numbers in 200–210 outside the range'), cap);
  const bys = await page.locator('.jw-review-row', {hasText: 'Bystander 175'}).textContent();
  check('outsider relocated via exclusive rule', bys.includes('175 → 200') && bys.includes('outside the exclusive range'), bys);
  await stageFromSheet(api, 'jw-sheet-organize');
  await applyArrangement(api, REVISION + 1);
  const doc = await api.doc();
  const num = (id) => doc.channels.find((c) => c.id === id).number;
  check('all outsiders relocated, block packed',
    num('net_c0000003') === 200 && num('net_c0000002') === 172 && num('net_c0000004') === 173 && num('net_c0000001') === 171,
    {b175: num('net_c0000003'), b173: num('net_c0000002'), b180: num('net_c0000004'), b171: num('net_c0000001')});
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 6. Create group + assign in ONE Apply (R5 territory).
async function flow6(browser) {
  const api = await boot(browser);
  const {page} = api;
  // (a) editor inline "Create group…" — the group rides the same Apply packet
  await openEditor(api, 'Chart One');
  await page.locator('#f-group').selectOption('__create__');
  await page.locator('input[aria-label="New group name"]').last().fill('Fresh Crew');
  await page.locator('#apply-btn').click();
  await page.locator('.jw-apply-state', {hasText: 'Applied at r'}).waitFor({timeout: 30000});
  check('R5a: editor Create group… commits in one Apply',
    api.state.applies.length === 1 && api.state.applies[0].receipt.status === 'committed',
    api.state.applies.map((a) => a.receipt.status));
  check('R5a: client group id uses the grp_ prefix (storage-compatible)',
    api.state.applies[0].ops.some((op) => op.op === 'group.put' && /^grp_[0-9a-f]{8}$/.test(op.group.id)),
    api.state.applies[0].ops.filter((o) => o.op === 'group.put'));
  const doc = await api.doc();
  const crew = doc.groups.find((g) => g.name === 'Fresh Crew');
  check('R5a: group created + assigned', !!crew &&
    doc.channels.find((c) => c.id === 'net_a0000001').groupId === crew.id,
    {groups: doc.groups.map((g) => g.name), chartOne: (doc.channels.find((c) => c.id === 'net_a0000001') || {}).groupId});
  // (b) create-at-occupied referencing a packet-created group: the sheet's
  // overlay references an uncommitted group, and the preview must model it
  // via the "groups" argument (R5) — no unknown_group error.
  await openNewChannel(api, {number: 300, name: 'Renumbered Crew', createGroup: 'Renumbered Crew'});
  await page.getByRole('button', {name: 'Create draft'}).click();
  await page.locator('.jw-sheet-numres').waitFor();
  await settle(1800);
  const sheetErrs = await page.locator('.jw-sheet-numres .jw-error-text').allTextContents();
  check('R5b: occupied create with a new group previews cleanly (no unknown_group)',
    sheetErrs.length === 0, sheetErrs);
  check('R5b: preview carried the pending group via the groups argument',
    api.state.previews.length > 0 &&
    api.state.previews.some((p) => p.groups.some((g) => g.name === 'Renumbered Crew' && /^grp_/.test(g.id))),
    api.state.previews.map((p) => p.groups));
  check('R5b: UX4 — the sheet demands complete rules before placing a bare draft',
    await page.locator('.jw-sheet-numres .jw-note-warn', {hasText: 'rules'}).count() === 1 &&
    await stageButton(api, 'jw-sheet-numres').isDisabled());
  await page.getByRole('button', {name: 'Cancel — keep drafts'}).click();
  await settle(400);
  await authorTagRule(api, 'Tag 2');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  const sheetErrs2 = await page.locator('.jw-sheet-numres .jw-error-text').allTextContents();
  check('R5b: reopened placement sheet still previews the pending group cleanly',
    sheetErrs2.length === 0, sheetErrs2);
  await stageFromSheet(api, 'jw-sheet-numres');
  await applyArrangement(api, REVISION + 2);
  check('R5b: ONE Apply creates the group AND places the channel',
    api.state.applies.length === 2 &&
    api.state.applies[1].ops[0].op === 'group.put' &&
    api.state.applies[1].ops.some((op) => op.op === 'channel.create'),
    api.state.applies[1].ops);
  const doc2 = await api.doc();
  const crew2 = doc2.groups.find((g) => g.name === 'Renumbered Crew');
  const placed = crew2 ? doc2.channels.find((c) => c.groupId === crew2.id) : null;
  check('R5b: group + channel committed together at 300',
    !!crew2 && /^grp_/.test(crew2.id) && placed && placed.number === 300 &&
    doc2.channels.find((c) => c.name === 'Occupied 302').number === 303,
    {crew2: crew2 && crew2.id, placed: placed && placed.number});
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 7. Revision conflict between stage and apply (driver bumps); lost-response
//    replay with the same requestId.
async function flow7(browser) {
  const api = await boot(browser);
  const {page} = api;
  // a bystander content draft proves drafts survive the conflict
  await openEditor(api, 'Chart Three');
  await page.locator('#f-name').fill('Zeta Three');
  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('303');
  await page.locator('.jw-hint', {hasText: '303 is free.'}).waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  const firstRequestId = (await api.org()).staged.packet.requestId;

  api.armConflictBump();
  await arrangeBar(api).getByRole('button', {name: /^Apply/}).click();
  await page.locator('.jw-toast', {hasText: 'Revision conflict'}).first().waitFor({timeout: 30000});
  check('conflict receipt recorded', api.state.applies[0].receipt.error === 'revision_conflict' &&
    api.state.applies[0].receipt.currentRevision === REVISION + 1, api.state.applies[0].receipt);
  check('other writer bumped the revision', await api.rev() === REVISION + 1, await api.rev());
  const stale = await page.locator('.jw-arrange-stale').textContent();
  check('bar shows stale base + Reload and replan', stale.includes('Based on r' + REVISION) && stale.includes('r' + (REVISION + 1)) &&
    await page.getByRole('button', {name: 'Reload and replan'}).count() === 1, stale);
  const drafts = await api.drafts();
  check('content drafts retained through the conflict',
    drafts && drafts.net_a0000003 && drafts.net_a0000003.draft.name === 'Zeta Three', drafts && drafts.net_a0000003);

  await page.getByRole('button', {name: 'Reload and replan'}).click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  check('replan re-previews at the new revision',
    api.state.previews.length > 0 && api.state.previews[api.state.previews.length - 1].valid === true);
  await stageFromSheet(api, 'jw-sheet-numres');
  const org = await api.org();
  check('re-staged against the fresh revision with a NEW requestId',
    org.staged.baseRevision === REVISION + 1 && org.staged.packet.requestId !== firstRequestId,
    {base: org.staged.baseRevision, req: org.staged.packet.requestId});

  // lost response: the submit never reaches the server; the frozen requestId
  // must be kept and offered as Check result / Retry same Apply (R6).
  api.armAbort();
  await arrangeBar(api).getByRole('button', {name: /^Apply/}).click();
  await waitUnknownOutcome(api);
  check('R6: transport failure keeps the packet + requestId and offers Check/Retry',
    api.state.applies.length === 1 && !!(await api.org()).pending, {
      applies: api.state.applies.length,
      pending: !!(await api.org()).pending,
      pill: await applyingPill(api).count(),
    });
  const unknownRequestId = (await api.org()).pending.requestId;
  await arrangeBar(api).getByRole('button', {name: 'Check result'}).click();
  await page.locator('.jw-toast', {hasText: 'Still no result'}).first().waitFor({timeout: 15000});
  check('R6: honest unknown after Check result (never submitted)',
    (await api.org()).pending && (await api.org()).pending.requestId === unknownRequestId &&
    api.state.applies.length === 1);
  await arrangeBar(api).getByRole('button', {name: 'Retry same Apply'}).click();
  await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
  const mine = api.state.applies.filter((a) => a.requestId === unknownRequestId);
  check('same requestId resubmitted', mine.length === 1 && mine[0].requestId === unknownRequestId,
    mine.map((m) => m.requestId));
  check('revision advanced exactly once', await api.rev() === REVISION + 2 && await api.number('Chart One') === 303,
    {rev: await api.rev(), chartOne: await api.number('Chart One')});
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 8. Edit-while-in-flight; bulk/editor serialization; undo/redo; bystander
//    draft rebase; stage reversal; stage-while-pending (F4).
async function flow8(browser) {
  const api = await boot(browser);
  const {page} = api;

  // (i) newer edits stay draft while a snapshot applies
  await openEditor(api, 'Chart Two');
  await page.locator('#f-name').fill('Chart Two v1');
  api.armHoldApply(2500);
  await page.locator('#apply-btn').click();
  await page.locator('.jw-apply-state', {hasText: 'Applying…'}).waitFor({timeout: 10000});
  await page.locator('#f-name').fill('Chart Two v2');
  await page.locator('.jw-toast', {hasText: 'newer edits are still draft'}).first().waitFor({timeout: 30000});
  check('first snapshot committed, second edit still draft',
    await api.number('Chart Two v1') !== null && (await api.drafts()).net_a0000002.draft.name === 'Chart Two v2',
    {server: await api.number('Chart Two v1'), draft: (await api.drafts()).net_a0000002.draft.name});

  // (8b) R9 (was known-fail F5): the editor Apply rides the SAME submission
  // coordinator — a bulk action queued during its flight serializes behind
  // it and never self-inflicts a revision conflict.
  api.armHoldApply(4500);
  await page.locator('#apply-btn').click();
  await page.locator('.jw-apply-state', {hasText: 'Applying…'}).waitFor({timeout: 10000});
  await ensureBulkMode(api);
  await selectRows(api, ['Chart One']);
  await page.locator('.jw-bulk-bar').getByRole('button', {name: 'Pause', exact: true}).click();
  await page.locator('.jw-overlay .jw-dialog').getByRole('button', {name: 'Apply', exact: true}).click();
  for (let i = 0; i < 40; i++) {
    const one = ((await api.doc()).channels.find((c) => c.id === 'net_a0000001') || {});
    if (one.paused === true && (await api.rev()) === REVISION + 3) break;
    await settle(500);
  }
  const conflictToast = await page.locator('.jw-toast', {hasText: 'Revision conflict'}).count();
  const editorConflict = await page.locator('.jw-apply-state', {hasText: 'Revision conflict'}).count();
  check('R9: no self-inflicted revision conflict inside one tab', conflictToast === 0 && editorConflict === 0,
    {bulkConflictToast: conflictToast, editorConflictBar: editorConflict});
  check('R9: both submissions committed, the second at the first receipt\'s revision',
    api.state.applies.length === 3 &&
    api.state.applies[1].receipt.status === 'committed' && String(api.state.applies[1].expected) === String(REVISION + 1) &&
    api.state.applies[2].receipt.status === 'committed' && String(api.state.applies[2].expected) === String(REVISION + 2),
    api.state.applies.map((a) => ({e: a.expected, s: a.receipt.status})));
  check('bulk path landed (Chart One paused)', ((await api.doc()).channels.find((c) => c.id === 'net_a0000001') || {}).paused === true);

  // (ii) undo/redo of staging (UX7: the bar persists as an empty state)
  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('303');
  await page.locator('.jw-hint', {hasText: '303 is free.'}).waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Undo'}).click();
  check('undo clears the staging (bar unmounts or drops to its empty state)',
    (await page.locator('.jw-arrange-bar:not(.jw-arrange-bar-empty)').count()) === 0);
  // redo is reachable while an organize sheet holds the keyboard scope
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.keyboard.press('Control+Shift+z');
  await settle(400);
  await page.getByRole('button', {name: 'Close — keep staging'}).click();
  check('redo restores the staged bar',
    await page.locator('.jw-arrange-bar.jw-arrange-bar-empty').count() === 0 &&
    (await arrangeBar(api).textContent()).includes('303'));
  await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Discard'}).click();
  await page.locator('.jw-overlay .jw-dialog').getByRole('button', {name: 'Discard'}).click();
  await settle(300);

  // (iii) bystander content draft silently rebased; external-change notice
  await openEditor(api, 'Chart Three');
  await page.locator('#f-name').fill('Zeta Three');
  await ensureBulkMode(api);
  await page.locator('.jw-bulk-bar').getByRole('button', {name: 'Clear', exact: true}).click();
  await selectRows(api, ['Chart Three']);
  await page.getByRole('button', {name: 'Organize selection…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'Move block to start'}).click();
  await page.locator('#jw-org-bstart').fill('302');
  await waitReview(api, 2);
  await stageFromSheet(api, 'jw-sheet-organize');
  await applyArrangement(api);
  await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first()
    .getByRole('button', {name: 'Stage reversal'}).click();
  await page.locator('.jw-arrange-bar', {hasText: 'Reversal of:'}).waitFor({timeout: 15000});
  const d3 = (await api.drafts()).net_a0000003;
  check('bystander draft number silently rebased (299→302)', d3.draft.number === 302 && d3.draft.name === 'Zeta Three',
    d3 && d3.draft);
  await settle(600); // the editor is still mounted on Chart Three (draft head shows "Zeta Three")
  check('no rebase notice for a non-conflicting rebase', await page.locator('.jw-rebase-note').count() === 0);

  // (iv) Stage reversal after commit: inverse map, new Apply — BEFORE the
  // external-rename steps (a revision bump in between would stale the bar).
  await applyArrangement(api);
  check('reversal restored 299/302', await api.number('Chart Three') === 299 && await api.number('Occupied 302') === 302,
    {c3: await api.number('Chart Three'), o302: await api.number('Occupied 302')});
  check('reversal rebased the bystander draft back', ((await api.drafts()).net_a0000003.draft.number) === 299);

  // external same-field change → the editor's three-way rebase NAMES it
  await api.drv('rename', {channelId: 'net_a0000003', name: 'Server Renamed'});
  await page.getByRole('button', {name: 'More actions'}).click();
  await page.getByRole('menuitem', {name: 'Reload library'}).click();
  await page.locator('.jw-toast', {hasText: 'Library reloaded'}).first().waitFor({timeout: 20000});
  await page.locator('.jw-rebase-note').waitFor({timeout: 15000});
  check('same-field conflict names itself', (await page.locator('.jw-rebase-note').textContent()).includes('The server also changed name'));
  check('draft keeps the local value', ((await api.drafts()).net_a0000003.draft.name) === 'Zeta Three');

  // (8c) R2 (was known-fail F4): staging while an arrangement Apply is in
  // flight — the fixed frontend REFUSES it (F3 guard); the in-flight plan
  // commits and no draft ever sees an uncommitted map.
  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('305');
  await page.locator('.jw-hint', {hasText: '305 is free.'}).waitFor({timeout: 15000});
  await page.locator('.jw-sheet-numres button.jw-btn-primary:not([disabled])').waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  api.armHoldApply(6000);
  await arrangeBar(api).getByRole('button', {name: /^Apply/}).click();
  await applyingPill(api).waitFor({timeout: 5000});
  // second arrangement DURING the flight: staged, but its Apply-start Stage
  // would refuse — assert the guard refuses the STAGE itself.
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  // part (iii) left a move_block cached form — restore the range action first
  await page.getByRole('button', {name: 'Arrange within range'}).click();
  await page.getByRole('button', {name: 'A number interval'}).click();
  await page.locator('#jw-org-scstart').fill('298');
  await page.locator('#jw-org-scend').fill('299');
  await page.locator('#jw-org-rstart').fill('400');
  await page.locator('#jw-org-rend').fill('401');
  await waitReview(api, 2);
  await page.locator('.jw-sheet-organize button.jw-btn-primary').click();
  await page.locator('.jw-toast', {hasText: 'still unresolved'}).first().waitFor({timeout: 10000});
  check('R2/F4: the during-flight Stage is refused — the submitted plan is never overwritten',
    (await page.locator('.jw-sheet-organize').count()) === 1 &&
    ((await api.org()).staged.label || '').includes('305'));
  await page.getByRole('button', {name: 'Close — keep staging'}).click();
  await settle(4500); // let the held apply land
  await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
  check('server committed the ORIGINAL in-flight plan', (await api.number('Chart One')) === 305, await api.number('Chart One'));
  const d3c = (await api.drafts()).net_a0000003;
  check('R2: bystander drafts untouched by the refused plan (299, Zeta Three)',
    d3c && d3c.draft.number === 299 && d3c.draft.name === 'Zeta Three', d3c && d3c.draft);
  const orgC = await api.org();
  check('R2: pending resolved; nothing of the refused staging leaked',
    orgC.pending === null, {pending: !!orgC.pending});
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 9. Gating: without features.arrangement the organize surface explains
//    itself; basic editing keeps working.
async function flow9(browser) {
  const api = await boot(browser, {arrangement: false});
  const {page} = api;
  check('gate pill explains the old backend', await page.locator('.jw-arrange-gate').count() === 1 &&
    (await page.locator('.jw-arrange-gate').textContent()).includes('Arrangement needs a plugin update'));
  check('no organize entries', await page.getByRole('button', {name: 'Organize channels…'}).count() === 0 &&
    await page.getByRole('button', {name: 'Organize selection…'}).count() === 0);
  await openGroupMenu(api, 'Performers');
  check('group menu lacks Arrange numbers…', await page.getByRole('menuitem', {name: 'Arrange numbers…'}).count() === 0);
  await page.keyboard.press('Escape');
  await openEditor(api, 'Chart Three');
  await page.locator('#f-number').fill('300');
  await settle(300);
  check('occupied number explains without Resolve', (await page.locator('#err-number').textContent()).includes('Pick a free number'));
  check('no Resolve conflict… link', await page.getByRole('button', {name: 'Resolve conflict…'}).count() === 0);
  check('no Move / insert… entry', await page.getByRole('button', {name: 'Move / insert…'}).count() === 0);

  await openNewChannel(api, {number: 300, name: 'Gate Test'});
  await page.getByRole('button', {name: 'Create draft'}).click();
  check('new-channel refuses occupied numbers when gated',
    (await page.locator('.jw-dialog .jw-error-text').textContent()).includes('Number 300 is not free in 100–899.'));
  await page.getByRole('button', {name: 'Cancel'}).click();

  await openEditor(api, 'Chart One');
  await page.locator('#f-name').fill('Chart One Edited');
  await page.locator('#apply-btn').click();
  await page.locator('.jw-apply-state', {hasText: 'Applied at r'}).waitFor({timeout: 30000});
  check('basic editing still applies', await api.number('Chart One Edited') === 297);
  await page.getByRole('button', {name: 'Groups…'}).click();
  await page.locator('.jw-groups-row').first().waitFor();
  await page.locator('input[aria-label="Group name Alpha"]').fill('Alpha Renamed');
  await page.locator('.jw-dialog-foot').getByRole('button', {name: 'Apply'}).click();
  await page.locator('.jw-groups-row').first().waitFor({state: 'detached', timeout: 30000});
  await settle(800);
  const doc = await api.doc();
  check('groups still apply', doc.groups.some((g) => g.name === 'Alpha Renamed'));
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 10. Visual sweep + geometry + keyboard traversal (dark + light).
async function flow10(browser) {
  const api = await boot(browser);
  const {page} = api;
  await api.shot('dark-01-main');
  check('dial rows render at 44px', await page.evaluate(() => {
    const row = document.querySelector('.jw-dial-row');
    return row ? row.offsetHeight : 0;
  }) === 44, await page.evaluate(() => document.querySelector('.jw-dial-row') && document.querySelector('.jw-dial-row').offsetHeight));

  await openNewChannel(api, {number: 300, name: 'Visual One'});
  await page.getByRole('button', {name: 'Create draft'}).click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  await api.shot('dark-02-numres-sheet');
  check('focus starts inside the sheet', await page.evaluate(() =>
    !!document.activeElement && !!document.activeElement.closest('.jw-dialog')));
  for (let i = 0; i < 5; i++) await page.keyboard.press('Tab');
  check('focus stays trapped in the sheet after 5 Tabs', await page.evaluate(() =>
    !!document.activeElement && !!document.activeElement.closest('.jw-dialog')));
  await page.keyboard.press('Escape');
  await settle(250);

  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'An entire group'}).click();
  await page.locator('#jw-org-group').selectOption('grp_alpha001');
  await page.locator('#jw-org-rstart').fill('290');
  await page.locator('#jw-org-rend').fill('310');
  await waitReview(api, 1);
  await api.shot('dark-03-organize-review');
  // R11: header and row cells must share the same four grid columns —
  // Channel | Number | Group | Why — no cell slipping under the wrong head.
  const cols = await page.evaluate(() => {
    const x = (sel) => { const n = document.querySelector(sel); return n ? Math.round(n.getBoundingClientRect().left) : null; };
    return {
      head: [x('.jw-review-h-channel'), x('.jw-review-h-move'), x('.jw-review-h-group'), x('.jw-review-h-reason')],
      row: [x('.jw-review-row .jw-review-channel'), x('.jw-review-row .jw-review-move'),
            x('.jw-review-row .jw-review-group'), x('.jw-review-row .jw-review-reason')],
      rowH: (() => { const n = document.querySelector('.jw-review-row'); return n ? n.offsetHeight : null; })(),
    };
  });
  check('R11: review columns align with the header (Channel/Number/Group/Why)',
    cols.head.every((hx, i) => hx != null && cols.row[i] != null && Math.abs(hx - cols.row[i]) <= 2) &&
    cols.rowH != null && cols.rowH <= 40,
    cols);
  await stageFromSheet(api, 'jw-sheet-organize');
  await api.shot('dark-04-staged-bar');
  await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Discard'}).click();
  await page.locator('.jw-overlay .jw-dialog').getByRole('button', {name: 'Discard'}).click();
  await settle(300);

  await page.setViewportSize({width: 720, height: 900});
  await settle(400);
  await page.getByRole('button', {name: 'Actions ▾'}).click();
  await settle(250);
  await api.shot('dark-05-narrow-actions');
  await page.keyboard.press('Escape');
  await page.setViewportSize({width: 1500, height: 950});
  await page.evaluate(() => { document.body.style.zoom = '2'; });
  await settle(400);
  await api.shot('dark-06-zoom-200');
  await page.evaluate(() => { document.body.style.zoom = ''; });
  check('no page errors (dark sweep)', api.state.errors.length === 0, api.state.errors);
  await api.context.close();

  const light = await boot(browser, {theme: 'light'});
  await light.shot('light-01-main');
  await light.page.getByRole('button', {name: 'Organize channels…'}).click();
  await light.page.locator('.jw-sheet-organize').waitFor();
  await light.page.getByRole('button', {name: 'An entire group'}).click();
  await light.page.locator('#jw-org-group').selectOption('grp_alpha001');
  await light.page.locator('#jw-org-rstart').fill('290');
  await light.page.locator('#jw-org-rend').fill('310');
  await waitReview(light, 1);
  await light.shot('light-02-organize-review');
  await stageFromSheet(light, 'jw-sheet-organize');
  await light.shot('light-03-staged-bar');
  check('no page errors (light sweep)', light.state.errors.length === 0, light.state.errors);
  await light.context.close();
}

// 11. extras/channel-studio-custom.js alongside: decorate-only, core Apply
//     still coordinator-driven; removal cleans up.
async function flow11(browser) {
  const api = await boot(browser);
  const {page} = api;
  await page.addScriptTag({content: EXTRAS_SRC}); // Stash re-injection semantics
  await settle(600);
  check('snippet attached to the live page state', await page.evaluate(() =>
    !!(window.JWChannelStudio && window.JWChannelStudio.version >= 1)));
  const stagedBefore = api.state.console.filter((c) => c.includes('arrangement staged')).length;

  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('303');
  await page.locator('.jw-hint', {hasText: '303 is free.'}).waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  check('snippet sees the staged event', api.state.console.filter((c) => c.includes('arrangement staged')).length === stagedBefore + 1);
  check('snippet badges the body', await page.evaluate(() => document.body.classList.contains('jw-owner-has-staged')));

  // "remove": re-register the same name with a no-op → prior cleanup runs
  await page.evaluate(() => {
    window.JWChannelStudio.registerExtension('channel-studio-owner-demo', {version: 1, setup: () => function () {}});
  });
  await settle(300);
  check('removal clears the badge', await page.evaluate(() => !document.body.classList.contains('jw-owner-has-staged')));
  const stagedBefore2 = api.state.console.filter((c) => c.includes('arrangement staged')).length;
  await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Undo'}).click();
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.keyboard.press('Control+Shift+z');
  await settle(300);
  await page.getByRole('button', {name: 'Close — keep staging'}).click();
  check('removed snippet receives no further events',
    api.state.console.filter((c) => c.includes('arrangement staged')).length === stagedBefore2);

  // core Apply still goes through the one coordinator
  await applyArrangement(api, REVISION + 1);
  check('core arrangement Apply committed through the coordinator',
    api.state.applies.length === 1 && api.state.applies[0].receipt.status === 'committed' &&
    await api.number('Chart One') === 303, api.state.applies);
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 12. R1: Stage can never freeze a stale plan — synchronous unbinding,
//     reordered/delayed preview responses, and the numres sheet's debounce.
async function flowR1(browser) {
  const api = await boot(browser);
  const {page} = api;
  // -- variant A: immediate Stage after a range change must stage the NEW plan
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'An entire group'}).click();
  await page.locator('#jw-org-group').selectOption('grp_alpha001');
  await page.locator('#jw-org-rstart').fill('400');
  await page.locator('#jw-org-rend').fill('410');
  await waitReview(api, 6);
  check('400–410 plan is bound and stageable', await stageButton(api, 'jw-sheet-organize').isEnabled());
  // any input change invalidates the binding SYNCHRONOUSLY — before the
  // 350ms debounce fires the replacement preview.
  await page.locator('#jw-org-rstart').fill('500');
  await page.locator('#jw-org-rend').fill('510');
  check('R1: Stage is dead the instant an input changes (old response unbound)',
    await stageButton(api, 'jw-sheet-organize').isDisabled());
  await settle(600);
  check('R1: the abandoned 400 plan was never staged in the click window',
    (await page.locator('.jw-arrange-bar:not(.jw-arrange-bar-empty)').count()) === 0);
  await waitReview(api, 6);
  check('R1: the 500–510 preview replaced the review',
    (await page.locator('.jw-review-row').first().textContent()).includes('500'));
  await stageFromSheet(api, 'jw-sheet-organize');
  const stagedA = await api.org();
  const renumA = stagedA.staged.packet.ops.find((o) => o.op === 'channels.renumber');
  check('R1: staged packet assigns 500.., never the abandoned 400s',
    renumA && renumA.assignments[0].number === 500 && renumA.assignments[1].number === 501,
    renumA && renumA.assignments.slice(0, 2));
  check('R1: staged label names the NEW destination',
    stagedA.staged.label.includes('500–510'), stagedA.staged.label);
  await arrangeBar(api).getByRole('button', {name: 'Undo'}).click();
  await settle(300);

  // -- variant B: a slow OLD response landing late must be discarded
  // (Stage closed the sheet; UX7's cached form restores scope + range.)
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await settle(600);
  api.armPreviewDelay(({intent}) => !!(intent && intent.range && intent.range.start === 400), 2600);
  await page.locator('#jw-org-rstart').fill('400');
  await page.locator('#jw-org-rend').fill('410');
  await settle(700); // the 400 preview FIRES and is held in flight
  await page.locator('#jw-org-rstart').fill('500');
  await page.locator('#jw-org-rend').fill('510');
  await settle(900); // the CURRENT (500) preview fires and lands first
  check('R1: current plan (500) rendered while the 400 response is still held',
    (await page.locator('.jw-review-row').first().textContent()).includes('500'));
  await settle(2300); // the held 400 response lands LATE
  check('R1: precondition — the held 400 response did arrive late',
    api.state.previews.filter((p) => p.intent.range && p.intent.range.start === 400).length >= 2,
    api.state.previews.map((p) => p.intent.range));
  check('R1: the late response was discarded — review still shows the 500 plan',
    (await page.locator('.jw-review-row').first().textContent()).includes('500'));
  check('R1: still bound after the late landing (Stage enabled)',
    await stageButton(api, 'jw-sheet-organize').isEnabled());
  await stageFromSheet(api, 'jw-sheet-organize');
  const stagedB = await api.org();
  const renumB = stagedB.staged.packet.ops.find((o) => o.op === 'channels.renumber');
  check('R1: staged packet after the late landing still assigns 500..',
    renumB && renumB.assignments[0].number === 500, renumB && renumB.assignments.slice(0, 2));
  await arrangeBar(api).getByRole('button', {name: 'Discard'}).click();
  await page.locator('.jw-overlay .jw-dialog').getByRole('button', {name: 'Discard'}).click();
  await settle(300);

  // -- variant C: the number-resolution sheet obeys the same rule
  await page.keyboard.press('Escape');
  await settle(300);
  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('303');
  // the "303 is free." hint is client-side; BOUND means the response landed
  await page.locator('.jw-sheet-numres button.jw-btn-primary:not([disabled])').waitFor({timeout: 15000});
  check('R1: numres 303 plan stageable', await stageButton(api, 'jw-sheet-numres').isEnabled());
  await page.locator('#jw-numres-dest').fill('305');
  check('R1: numres Stage dies the instant the destination changes',
    await stageButton(api, 'jw-sheet-numres').isDisabled());
  await settle(700);
  check('R1: numres old (303) plan not staged in the debounce window',
    (await page.locator('.jw-arrange-bar:not(.jw-arrange-bar-empty)').count()) === 0);
  await page.locator('.jw-hint', {hasText: '305 is free.'}).waitFor({timeout: 15000});
  check('R1: numres new (305) plan bound', await stageButton(api, 'jw-sheet-numres').isEnabled());
  await stageFromSheet(api, 'jw-sheet-numres');
  const stagedC = await api.org();
  const renumC = stagedC.staged.packet.ops.find((o) => o.op === 'channels.renumber');
  check('R1: numres staged packet places at the CURRENT destination (305)',
    renumC && renumC.assignments.some((a) => a.channelId === 'net_a0000001' && a.number === 305),
    renumC && renumC.assignments);
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 13. R2: completing an Apply reconciles ONLY its submitted snapshot. The
//     fixed frontend REFUSES a new Stage while an Apply is unresolved (F3
//     guard), so the "newer plan during flight" path is Undo: staged flips
//     back to the previous plan while the submitted packet stays immutable.
async function flowR2(browser) {
  const api = await boot(browser);
  const {page} = api;
  await openEditor(api, 'Chart Three');
  await page.locator('#f-name').fill('Zeta Three'); // bystander content draft
  // plan A (kept on the undo stack) …
  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('305');
  await page.locator('.jw-hint', {hasText: '305 is free.'}).waitFor({timeout: 15000});
  await page.locator('.jw-sheet-numres button.jw-btn-primary:not([disabled])').waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  // … and plan B (the bar's current plan, the one Apply will submit)
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'A number interval'}).click();
  await page.locator('#jw-org-scstart').fill('298');
  await page.locator('#jw-org-scend').fill('299');
  await page.locator('#jw-org-rstart').fill('400');
  await page.locator('#jw-org-rend').fill('401');
  await waitReview(api, 2);
  await stageFromSheet(api, 'jw-sheet-organize');
  check('B is the bar\'s staged plan (A sits on the undo stack)',
    ((await api.org()).staged.label || '').includes('400–401'));
  const tokenB = (await api.org()).staged.correlationToken;
  api.armHoldApply(6000);
  await arrangeBar(api).getByRole('button', {name: /^Apply/}).click();
  await applyingPill(api).waitFor({timeout: 5000});
  // the F3 guard: a NEW Stage during flight is refused, never a silent
  // overwrite of the unresolved submitted packet.
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'Assign group', exact: true}).click();
  await page.locator('#jw-org-dest').selectOption('grp_trio001');
  await waitReview(api, 2);
  await page.locator('.jw-sheet-organize button.jw-btn-primary').click();
  await page.locator('.jw-toast', {hasText: 'still unresolved'}).first().waitFor({timeout: 10000});
  check('R2/F3: a new Stage during the flight is REFUSED (sheet stays open, staged untouched)',
    (await page.locator('.jw-sheet-organize').count()) === 1 &&
    (await api.org()).staged.correlationToken === tokenB);
  await page.getByRole('button', {name: 'Close — keep staging'}).click();
  await settle(300);
  // Undo during flight: the bar flips to plan A; the SUBMITTED packet (B)
  // stays immutable in pending.
  await arrangeBar(api).getByRole('button', {name: 'Undo'}).click();
  await settle(300);
  check('R2: Undo during flight shows plan A while B\'s Apply runs',
    ((await api.org()).staged.label || '').includes('305') && !!(await api.org()).pending,
    {staged: (await api.org()).staged.label, pending: !!(await api.org()).pending});
  await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
  await settle(600);
  check('server committed the SUBMITTED plan B only',
    (await api.number('Chart One')) === 297 &&
    (await api.number('Chart Two')) === 400 && (await api.number('Chart Three')) === 401,
    {one: await api.number('Chart One'), two: await api.number('Chart Two'), three: await api.number('Chart Three')});
  const orgAfter = await api.org();
  check('R2: the newer (undo-revealed) staging survives the commit — staged kept, pending resolved',
    orgAfter.staged && orgAfter.staged.intent && orgAfter.staged.intent.number === 305 &&
    orgAfter.staged.baseRevision === REVISION && orgAfter.pending === null,
    {staged: orgAfter.staged && orgAfter.staged.label, base: orgAfter.staged && orgAfter.staged.baseRevision,
     pending: !!orgAfter.pending});
  const d3 = (await api.drafts()).net_a0000003;
  check('R2: bystander draft keeps its content and adopts ONLY the committed map (401, never an uncommitted number)',
    d3 && d3.draft.name === 'Zeta Three' && d3.draft.number === 401, d3 && d3.draft);
  check('R2: bar flags the surviving staging stale + offers Reload and replan',
    (await page.locator('.jw-arrange-stale', {hasText: 'Based on r' + REVISION}).count()) === 1 &&
    (await page.getByRole('button', {name: 'Reload and replan'}).count()) === 1);
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 14. R3: newer creation edits remap onto the final id — never dropped.
async function flowR3(browser) {
  const api = await boot(browser);
  const {page} = api;
  await openNewChannel(api, {number: 300, name: 'Submitted channel name'});
  await page.getByRole('button', {name: 'Create draft'}).click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.getByRole('button', {name: 'Cancel — keep drafts'}).click();
  await settle(400);
  await authorTagRule(api, 'Tag 1');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  await stageFromSheet(api, 'jw-sheet-numres');
  const tempId = Object.keys((await api.drafts()) || {}).find((k) => String(k).startsWith('temp-'));
  api.armHoldApply(3000);
  await arrangeBar(api).getByRole('button', {name: /^Apply/}).click();
  await applyingPill(api).waitFor({timeout: 5000});
  // newer edit DURING the flight — after the packet was frozen at Stage
  await page.locator('#f-name').fill('Newer unsent channel name');
  await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
  await settle(600);
  const doc = await api.doc();
  const created = doc.channels.find((c) => c.name === 'Submitted channel name');
  check('server record carries the SUBMITTED name at 300',
    !!created && created.number === 300, created && {id: created.id, number: created.number});
  const drafts = await api.drafts();
  check('R3: drafts not emptied — the newer edit remapped onto the final id',
    !!created && drafts[created.id] && drafts[created.id].draft.name === 'Newer unsent channel name',
    created ? drafts[created.id] : 'no created channel');
  check('R3: the temp draft was dropped after the remap', !drafts[tempId], Object.keys(drafts));
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 15. R4: include-draft honors the reviewed group move — assignment-only AND
//     combined range+group variants.
async function flowR4(browser) {
  const api = await boot(browser);
  const {page} = api;
  // -- variant 1: assignment-only
  await openEditor(api, 'Chart One');
  await page.locator('#f-name').fill('Owner updated name');
  await ensureBulkMode(api);
  await selectRows(api, ['Chart One']);
  await page.getByRole('button', {name: 'Organize selection…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'Assign group', exact: true}).click();
  await page.locator('#jw-org-dest').selectOption('grp_trio001');
  await waitReview(api, 1);
  const row = page.locator('.jw-review-row', {hasText: 'Chart One'});
  check('review shows the reviewed move Alpha → Trio',
    (await row.textContent()).includes('Alpha → Trio'), await row.textContent());
  await row.getByRole('button', {name: 'include draft…'}).click();
  await settle(400);
  await stageFromSheet(api, 'jw-sheet-organize');
  await applyArrangement(api, REVISION + 1);
  const doc = await api.doc();
  const one = doc.channels.find((c) => c.id === 'net_a0000001');
  check('R4: committed group is the REVIEWED Trio (not the draft\'s stale Alpha)',
    one.groupId === 'grp_trio001', one);
  check('R4: dirty name committed alongside, number untouched',
    one.name === 'Owner updated name' && one.number === 297, {name: one.name, number: one.number});
  const put = api.state.applies[0].ops.find((op) => op.op === 'channel.put');
  check('R4: wire put carries the reconciled groupId (agreement rule extends to groups)',
    put && put.channel.groupId === 'grp_trio001', put && put.channel.groupId);
  await api.context.close();

  // -- variant 2: combined range + group
  const api2 = await boot(browser);
  const p2 = api2.page;
  await openEditor(api2, 'Chart Two');
  await p2.locator('#f-name').fill('Combined Name');
  await ensureBulkMode(api2);
  await selectRows(api2, ['Chart Two']);
  await p2.getByRole('button', {name: 'Organize selection…'}).click();
  await p2.locator('.jw-sheet-organize').waitFor();
  await p2.locator('#jw-org-rstart').fill('500');
  await p2.locator('#jw-org-rend').fill('500');
  await p2.locator('.jw-sheet-organize input[type="checkbox"]').check();
  await p2.locator('#jw-org-dest2').selectOption('grp_trio001');
  await waitReview(api2, 1);
  const row2 = p2.locator('.jw-review-row', {hasText: 'Chart Two'});
  check('combined review shows number AND group',
    (await row2.textContent()).includes('298 → 500') && (await row2.textContent()).includes('Alpha → Trio'),
    await row2.textContent());
  await row2.getByRole('button', {name: 'include draft…'}).click();
  await settle(400);
  await stageFromSheet(api2, 'jw-sheet-organize');
  await applyArrangement(api2, REVISION + 1);
  const doc2 = await api2.doc();
  const two = doc2.channels.find((c) => c.id === 'net_a0000002');
  check('R4 combined: number, group AND name all land as reviewed',
    two.number === 500 && two.groupId === 'grp_trio001' && two.name === 'Combined Name', two);
  check('no page errors (variant 2)', api2.state.errors.length === 0, api2.state.errors);
  await api2.context.close();
}

// 16. R5: inline group creation — editor path, occupied-create path, and bulk
//     create-and-assign, all with the pending group in preview AND packet.
async function flowR5(browser) {
  const api = await boot(browser);
  const {page} = api;
  // (a) editor inline creation
  await openEditor(api, 'Chart One');
  await page.locator('#f-group').selectOption('__create__');
  await page.locator('input[aria-label="New group name"]').last().fill('Fresh Crew');
  await page.locator('#apply-btn').click();
  await page.locator('.jw-apply-state', {hasText: 'Applied at r'}).waitFor({timeout: 30000});
  check('R5a: one Apply commits group.put + channel.put',
    api.state.applies.length === 1 &&
    api.state.applies[0].ops[0].op === 'group.put' && /^grp_/.test(api.state.applies[0].ops[0].group.id),
    api.state.applies[0].ops.map((o) => o.op));
  let doc = await api.doc();
  const crew = doc.groups.find((g) => g.name === 'Fresh Crew');
  check('R5a: group exists and Chart One is a member',
    !!crew && doc.channels.find((c) => c.id === 'net_a0000001').groupId === crew.id,
    doc.groups.map((g) => g.name));
  // (b) occupied-create referencing a NEW group: preview + packet composition
  await openNewChannel(api, {number: 300, name: 'Renumbered Crew', createGroup: 'Renumbered Crew'});
  await page.getByRole('button', {name: 'Create draft'}).click();
  await page.locator('.jw-sheet-numres').waitFor();
  await settle(1800);
  check('R5b: placement previews with the pending group (no unknown_group)',
    (await page.locator('.jw-sheet-numres .jw-error-text').count()) === 0,
    await page.locator('.jw-sheet-numres .jw-error-text').allTextContents());
  check('R5b: preview requests carried the groups argument',
    api.state.previews.some((p) => p.groups.some((g) => g.name === 'Renumbered Crew' && /^grp_/.test(g.id))),
    api.state.previews.map((p) => p.groups));
  await page.getByRole('button', {name: 'Cancel — keep drafts'}).click();
  await settle(400);
  await authorTagRule(api, 'Tag 2');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await waitChoices(api);
  check('R5b: reopened sheet still previews the pending group cleanly',
    (await page.locator('.jw-sheet-numres .jw-error-text').count()) === 0);
  await stageFromSheet(api, 'jw-sheet-numres');
  await applyArrangement(api, REVISION + 2);
  const bOps = api.state.applies[1].ops;
  check('R5b: packet order group.put → channel.create → renumber chain',
    bOps[0].op === 'group.put' && /^grp_/.test(bOps[0].group.id) &&
    bOps.some((o) => o.op === 'channel.create' && o.channel.groupId === bOps[0].group.id) &&
    bOps.some((o) => o.op === 'channels.renumber'),
    bOps.map((o) => o.op));
  doc = await api.doc();
  const crew2 = doc.groups.find((g) => g.name === 'Renumbered Crew');
  const placed = crew2 && doc.channels.find((c) => c.groupId === crew2.id);
  check('R5b: group + created channel committed together at 300',
    !!crew2 && placed && placed.number === 300 &&
    doc.channels.find((c) => c.name === 'Occupied 302').number === 303,
    {crew: crew2 && crew2.id, placed: placed && placed.number});
  // (c) bulk create-and-assign
  await ensureBulkMode(api);
  await selectRows(api, ['Chart Three', 'Occupied 300']);
  await page.getByRole('button', {name: 'Organize selection…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'Assign group', exact: true}).click();
  await page.locator('#jw-org-newgroup').fill('Bulk Crew');
  await waitReview(api, 2);
  await stageFromSheet(api, 'jw-sheet-organize');
  await applyArrangement(api, REVISION + 3);
  const cOps = api.state.applies[2].ops;
  check('R5c: bulk create-and-assign in one packet',
    cOps[0].op === 'group.put' && /^grp_/.test(cOps[0].group.id) &&
    cOps.some((o) => o.op === 'channels.move' && o.groupId === cOps[0].group.id),
    cOps.map((o) => o.op));
  doc = await api.doc();
  const crew3 = doc.groups.find((g) => g.name === 'Bulk Crew');
  check('R5c: both channels moved into the new group',
    !!crew3 && doc.channels.find((c) => c.id === 'net_a0000003').groupId === crew3.id &&
    doc.channels.find((c) => c.name === 'Occupied 300').groupId === crew3.id,
    crew3 && crew3.id);
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 17. R6: unknown-outcome recovery — transport abort BEFORE submission, and a
//     lost response AFTER a real commit.
async function flowR6(browser) {
  const api = await boot(browser);
  const {page} = api;
  // (a) the request never reaches the server
  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('303');
  await page.locator('.jw-hint', {hasText: '303 is free.'}).waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  api.armAbort();
  await arrangeBar(api).getByRole('button', {name: /^Apply/}).click();
  await waitUnknownOutcome(api);
  check('R6a: never stuck on "Applying…" — Check result / Retry offered',
    (await applyingPill(api).count()) === 0 && api.state.applies.length === 0,
    {pill: await applyingPill(api).count(), applies: api.state.applies.length});
  const reqA = (await api.org()).pending.requestId;
  check('R6a: the exact submitted packet + requestId are kept',
    !!(await api.org()).staged && (await api.org()).staged.packet.requestId === reqA, reqA);
  await arrangeBar(api).getByRole('button', {name: 'Check result'}).click();
  await page.locator('.jw-toast', {hasText: 'Still no result'}).first().waitFor({timeout: 15000});
  check('R6a: unknown stays actionable after Check result (never rejected, never committed)',
    !!(await api.org()).pending && (await api.org()).pending.requestId === reqA);
  await arrangeBar(api).getByRole('button', {name: 'Retry same Apply'}).click();
  await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
  check('R6a: retry committed ONCE under the kept requestId',
    api.state.applies.length === 1 && api.state.applies[0].requestId === reqA &&
    (await api.rev()) === REVISION + 1 && (await api.number('Chart One')) === 303,
    api.state.applies.map((a) => a.requestId));
  // (b) the response is lost AFTER the server committed
  await openEditor(api, 'Chart Two');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('304');
  await page.locator('.jw-hint', {hasText: '304 is free.'}).waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  api.armAbortAfter();
  await arrangeBar(api).getByRole('button', {name: /^Apply/}).click();
  await waitUnknownOutcome(api);
  check('R6b: the server DID commit — only this tab\'s outcome is unknown',
    api.state.applies.length === 2 && api.state.applies[1].receipt.status === 'committed' &&
    (await api.number('Chart Two')) === 304,
    {applies: api.state.applies.length, chartTwo: await api.number('Chart Two')});
  await arrangeBar(api).getByRole('button', {name: 'Check result'}).click();
  await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 15000});
  await settle(600);
  const orgB = await api.org();
  check('R6b: Check result reconciles the durable committed receipt (no resubmission)',
    orgB.pending === null && orgB.staged === null && api.state.applies.length === 2,
    {pending: !!orgB.pending, staged: !!orgB.staged, applies: api.state.applies.length});
  check('R6b: revision adopted', (await api.rev()) === REVISION + 2, await api.rev());
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 18. R7: Alphabetical ordering sends the canonical order:"name" and stages
//     via a real frontend-generated packet.
async function flowR7(browser) {
  const api = await boot(browser);
  const {page} = api;
  await openGroupMenu(api, 'Performers');
  await page.getByRole('menuitem', {name: 'Arrange numbers…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  check('task sheet fixes the group scope',
    (await page.locator('.jw-task-scope-line').textContent()).includes('Performers'));
  await page.locator('#jw-org-rstart').fill('300');
  await page.locator('#jw-org-rend').fill('699');
  await waitReview(api, 2);
  await page.getByRole('button', {name: 'Alphabetical (A–Z, ties by channel id)'}).click();
  await settle(1200);
  check('R7: review plans by name (Alpha Net first)',
    (await page.locator('.jw-review-row').first().textContent()).includes('Alpha Net'),
    await page.locator('.jw-review-row').allTextContents());
  const last = api.state.previews[api.state.previews.length - 1];
  check('R7: the frontend packet uses order:"name" (never the rejected "alpha")',
    last && last.intent.order === 'name', last && last.intent.order);
  await stageFromSheet(api, 'jw-sheet-organize');
  const renum = (await api.org()).staged.packet.ops.find((o) => o.op === 'channels.renumber');
  check('R7: staged packet packs Alpha Net → 303, Zulu Net → 304 (300–302 belong to Alpha bystanders)',
    renum && renum.assignments[0].number === 303 && renum.assignments[1].number === 304,
    renum && renum.assignments);
  await applyArrangement(api, REVISION + 1);
  check('R7: committed by name order',
    (await api.number('Alpha Net')) === 303 && (await api.number('Zulu Net')) === 304,
    {alpha: await api.number('Alpha Net'), zulu: await api.number('Zulu Net')});
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 19. R8: reload + replan reconstructs the COMPLETE authored intent — scope
//     group, order, destination, strategy, and the combined assignment.
async function flowR8(browser) {
  const api = await boot(browser);
  const {page} = api;
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'An entire group'}).click();
  await page.locator('#jw-org-group').selectOption('grp_perfo01');
  await page.locator('#jw-org-rstart').fill('400');
  await page.locator('#jw-org-rend').fill('410');
  await page.getByRole('button', {name: 'Alphabetical (A–Z, ties by channel id)'}).click();
  await page.locator('.jw-sheet-organize input[type="checkbox"]').check();
  await page.locator('#jw-org-dest2').selectOption('grp_trio001');
  await waitReview(api, 2);
  await stageFromSheet(api, 'jw-sheet-organize');
  check('staged: Performers → 400–410 + combined Trio assignment',
    ((await api.org()).staged.label || '').includes('400–410'), (await api.org()).staged.label);
  // an unrelated external revision makes the staged plan stale
  await api.drv('rename', {channelId: 'net_a0000003', name: 'Server Renamed'});
  await page.getByRole('button', {name: 'More actions'}).click();
  await page.getByRole('menuitem', {name: 'Reload library'}).click();
  await settle(900);
  check('bar flags the stale base after the external revision',
    (await page.locator('.jw-arrange-stale', {hasText: 'the library is now r' + (REVISION + 1)}).count()) === 1,
    await page.locator('.jw-arrange-stale').allTextContents());
  await page.reload();
  await page.locator('.jw-dial-row').first().waitFor({timeout: 30000});
  await settle(800);
  check('R8: the staged intent + stale flag survive the reload',
    (await page.locator('.jw-arrange-stale').count()) >= 1 &&
    ((await api.org()).staged || {}).baseRevision === REVISION);
  await page.getByRole('button', {name: 'Reload and replan'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  check('R8: scope group retained (Performers, NOT the first group)',
    (await page.locator('#jw-org-group').inputValue()) === 'grp_perfo01' &&
    (await page.locator('.jw-chip-active', {hasText: 'An entire group'}).count()) === 1,
    await page.locator('#jw-org-group').inputValue());
  check('R8: scope still counts 2 channels',
    (await page.locator('.jw-scope-line').first().textContent()).includes('2 channels in scope'));
  check('R8: order retained (Alphabetical)',
    (await page.getByRole('button', {name: 'Alphabetical (A–Z, ties by channel id)'}).getAttribute('aria-pressed')) === 'true');
  check('R8: conflict strategy retained (keep other channels in place)',
    (await page.getByRole('button', {name: 'Keep other channels in place'}).getAttribute('aria-pressed')) === 'true');
  check('R8: combined assignment retained (checkbox + Trio)',
    await page.locator('.jw-sheet-organize input[type="checkbox"]').isChecked() &&
    (await page.locator('#jw-org-dest2').inputValue()) === 'grp_trio001');
  check('R8: destination range retained (400–410)',
    (await page.locator('#jw-org-rstart').inputValue()) === '400' &&
    (await page.locator('#jw-org-rend').inputValue()) === '410');
  await waitReview(api, 2);
  const previews = api.state.previews;
  check('R8: replan re-previewed BOTH halves at the new revision with the kept settings',
    previews.length >= 2 && previews[previews.length - 1].expectedRevision === REVISION + 1 &&
    previews.some((p) => p.intent.type === 'assign_group' && p.intent.groupId === 'grp_trio001') &&
    previews.some((p) => p.intent.type === 'arrange_range' && p.intent.range &&
      p.intent.range.start === 400 && p.intent.order === 'name'),
    previews.slice(-2).map((p) => ({t: p.intent.type, rev: p.expectedRevision})));
  await stageFromSheet(api, 'jw-sheet-organize');
  const ops = (await api.org()).staged.packet.ops;
  check('R8: re-staged packet keeps the combined composition',
    ops.some((o) => o.op === 'channels.move' && o.groupId === 'grp_trio001') &&
    ops.some((o) => o.op === 'channels.renumber' && o.assignments.length === 2),
    ops.map((o) => o.op));
  await applyArrangement(api, REVISION + 2);
  check('R8: committed Alpha Net 400, Zulu Net 401',
    (await api.number('Alpha Net')) === 400 && (await api.number('Zulu Net')) === 401,
    {alpha: await api.number('Alpha Net'), zulu: await api.number('Zulu Net')});
  const doc = await api.doc();
  check('R8: both performers landed in Trio',
    doc.channels.find((c) => c.id === 'net_e0000002').groupId === 'grp_trio001' &&
    doc.channels.find((c) => c.id === 'net_e0000001').groupId === 'grp_trio001');
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 20. R9: editor Apply + bulk action serialize through the ONE coordinator —
//     the second op waits for the first receipt (no self-inflicted conflict).
async function flowR9(browser) {
  const api = await boot(browser);
  const {page} = api;
  await openEditor(api, 'Chart One');
  await page.locator('#f-name').fill('Serialized Name');
  api.armHoldApply(3000);
  await page.locator('#apply-btn').click();
  await page.locator('.jw-apply-state', {hasText: 'Applying…'}).waitFor({timeout: 10000});
  await ensureBulkMode(api);
  await selectRows(api, ['Chart One']);
  await page.locator('.jw-bulk-bar').getByRole('button', {name: 'Pause', exact: true}).click();
  await page.locator('.jw-overlay .jw-dialog').getByRole('button', {name: 'Apply', exact: true}).click();
  for (let i = 0; i < 40; i++) {
    const row = ((await api.doc()).channels.find((c) => c.id === 'net_a0000001') || {});
    if (row.paused === true && (await api.rev()) === REVISION + 2) break;
    await settle(500);
  }
  const row = (await api.doc()).channels.find((c) => c.id === 'net_a0000001');
  check('R9: both ops landed (rename + pause)',
    row.name === 'Serialized Name' && row.paused === true, row);
  check('R9: serialized — the bulk op ran at the editor receipt\'s revision',
    api.state.applies.length === 2 &&
    api.state.applies[0].receipt.status === 'committed' && String(api.state.applies[0].expected) === String(REVISION) &&
    api.state.applies[1].receipt.status === 'committed' && String(api.state.applies[1].expected) === String(REVISION + 1),
    api.state.applies.map((a) => ({e: a.expected, s: a.receipt.status, err: a.receipt.error})));
  check('R9: no revision-conflict toast anywhere',
    (await page.locator('.jw-toast', {hasText: 'Revision conflict'}).count()) === 0);
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// 21. R10: malformed payloads reject TYPED + DURABLY through the REAL
//     ValidateChannelChanges / ApplyChannelChanges — never a crash, never a
//     silent revision bump.
async function flowR10(browser) {
  const api = await boot(browser);
  const cases = [
    {name: 'assignments:true', ops: [{op: 'channels.renumber', assignments: true}], code: 'bad_assignment'},
    {name: 'assignments:42', ops: [{op: 'channels.renumber', assignments: 42}], code: 'bad_assignment'},
    {name: 'nested-array entry', ops: [{op: 'channels.renumber', assignments: [['net_a0000001', 300]]}], code: 'bad_assignment'},
    {name: 'kind:"invalid"', ops: [{op: 'channel.create', tempId: 'temp-review', channel: {kind: 'invalid', number: 100}}], code: 'bad_kind'},
  ];
  for (const c of cases) {
    const v = await api.drv('validate', {ops: JSON.stringify(c.ops)});
    check('R10 validate typed rejection: ' + c.name,
      v.valid === false && (v.errors || []).some((e) => e.code === c.code), v.errors);
    const doc = await api.doc();
    const receipt = await api.drv('apply', {
      requestId: 'harness-malformed-' + c.name.replace(/\W/g, ''),
      expectedRevision: String(doc.revision), ops: JSON.stringify(c.ops), actor: 'harness-malformed',
    });
    check('R10 apply rejected without crashing: ' + c.name,
      receipt.status === 'rejected' && receipt.error === 'validation_failed' &&
      (receipt.errors || []).some((e) => e.code === c.code), receipt);
    const durable = await api.drv('receipt', {requestId: receipt.requestId});
    check('R10 rejected receipt is durable + replay-exact: ' + c.name,
      durable && durable.status === 'rejected' && durable.digest === receipt.digest, durable);
  }
  const doc = await api.doc();
  check('R10: revision + definitions untouched by every malformed attempt',
    doc.revision === REVISION && doc.channels.length === 18,
    {revision: doc.revision, channels: doc.channels.length});
  check('no page errors', api.state.errors.length === 0, api.state.errors);
  await api.context.close();
}

// ----------------------------------------------------------------- main ----
(async () => {
  const browser = await chromium.launch({executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome',
    headless: true, args: ['--no-sandbox']});
  const only = process.env.FLOW ? parseInt(process.env.FLOW, 10) : null;
  const flows = [
    [1, 'create-at-occupied + shift chain + downward mirror', flow1],
    [2, 'existing 299 → 300 with dirty name (opt-in put + renumber)', flow2],
    [3, 'block 171/173/180 → 300 with bystanders', flow3],
    [4, 'Performers 300-699 useAvailable + capacity + shortfall + R7 alpha', flow4],
    [5, 'exclusive range with outside interval', flow5],
    [6, 'create group + assign in one Apply (R5)', flow6],
    [7, 'revision conflict + R6 unknown-outcome retry', flow7],
    [8, 'edit-in-flight, undo/redo, rebase, reversal, R2 stage-in-flight', flow8],
    [9, 'capability gating fallback', flow9],
    [10, 'visual sweep + geometry + keyboard', flow10],
    [11, 'extras snippet lifecycle', flow11],
    [12, 'R1: stale-preview binding, reorder, numres debounce', flowR1],
    [13, 'R2: submitted-snapshot reconciliation, newer staging survives', flowR2],
    [14, 'R3: newer creation edits remap onto the final id', flowR3],
    [15, 'R4: include-draft honors the reviewed group (both variants)', flowR4],
    [16, 'R5: inline group creation (editor, occupied-create, bulk)', flowR5],
    [17, 'R6: unknown outcome — abort before submit + lost-after-commit', flowR6],
    [18, 'R7: alphabetical order enum name', flowR7],
    [19, 'R8: reload + replan retains the full intent', flowR8],
    [20, 'R9: one coordinator serializes editor + bulk', flowR9],
    [21, 'R10: malformed payloads → typed + durable rejection', flowR10],
  ];
  for (const [n, title, fn] of flows) {
    if (only && n !== only) continue;
    currentFlow = n;
    log('\n== flow ' + n + ': ' + title);
    const t0 = Date.now();
    const knownBefore = Object.keys(report.failKnown).filter((k) => report.failKnown[k].some((x) => x.flow === n)).length;
    const newBefore = report.failNew.length;
    try {
      await fn(browser);
      const knownAfter = Object.keys(report.failKnown).filter((k) => report.failKnown[k].some((x) => x.flow === n)).length;
      const fixes = Object.keys(report.failKnown).filter((k) => report.failKnown[k].some((x) => x.flow === n));
      report.flowStatus[n] = knownAfter > knownBefore
        ? 'pass-with-known-failures(' + fixes.join(',') + ')'
        : (report.failNew.length > newBefore ? 'pass-with-new-findings' : 'pass');
    } catch (e) {
      report.flowStatus[n] = 'crashed';
      report.failHarness.push({flow: n, error: String((e && e.stack) || e).slice(0, 2000)});
      log('    FLOW CRASH: ' + String((e && e.message) || e));
    }
    log('   (' + Math.round((Date.now() - t0) / 1000) + 's)');
  }
  await browser.close();

  log('\n================ DRY-RUN SUMMARY ================');
  for (const [n, s] of Object.entries(report.flowStatus)) log('flow ' + n + ': ' + s);
  const knownCount = Object.values(report.failKnown).reduce((n, list) => n + list.length, 0);
  const gateFailures = report.failNew.length + knownCount + report.failHarness.length;
  log('known-classified failures: ' + JSON.stringify(Object.fromEntries(Object.entries(report.failKnown).map(([k, v]) => [k, v.length]))));
  log('new findings: ' + report.failNew.length + '; harness crashes: ' + report.failHarness.length +
    '; notes: ' + report.notes.length + '; shots: ' + report.shots.length);
  // R12 acceptance gate: ANY behavioral assertion failure (known- or
  // new-classified) or harness crash fails the process. STRICT is retired —
  // the gate is unconditional.
  log('GATE: ' + (gateFailures === 0
    ? 'PASS — 0 behavioral failures, 0 crashes'
    : 'FAIL — ' + gateFailures + ' failing assertion(s)/crash(es) (' +
      report.failNew.length + ' new, ' + knownCount + ' known-classified, ' + report.failHarness.length + ' crashes)'));
  fs.writeFileSync(path.join(SHOTS, 'dry-run-report.json'), JSON.stringify(
    Object.assign({}, report, {gate: {failures: gateFailures, pass: gateFailures === 0}}), null, 2));
  if (gateFailures > 0) process.exit(1);
  log('report + screenshots in ' + SHOTS);
})().catch((e) => { console.error(e); process.exit(1); });
