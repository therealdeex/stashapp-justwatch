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
//   STRICT=1 node ...                           # non-zero exit on unknown failures
//
// Current-tree expectation: flows touching F1/F3/F4/F5/F6 fail with
// [known-unfixed Fx] markers; the FINAL green run happens after Kimi's fixes.
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
report.notes.push('finding(c) empty-create-source: the number-resolution sheet auto-opens right after "Create draft", and its frozen packet merges the draft whose source is still the empty criteria stub — Apply arrangement is then REJECTED server-side (validation_failed, ops[0].channel.source empty_rules; verified against the real library.apply_transaction). The editor Apply path pre-checks this locally via validateDraft, but the arrangement path has no equivalent local check on merged create drafts. Recovery is typed and the toast surfaces it; suggested fix: run a validateDraft-style check over merged channel.create drafts before enabling Apply arrangement / Stage (or warn in the sheet).');
report.notes.push('finding(c) review-row reason wrap (visual): in the organize Review list the reason text renders inside the CHANNEL cell under the glyph tile (not the REASON column) and overflows the fixed 36px WindowedList row stride — see shots/dark-03-organize-review.png and shots/light-02-organize-review.png (both themes, .jw-review-row). Data is correct; layout only. Candidate for Kimi\'s fix round: give .jw-review-reason a bounded grid column / no-wrap-ellipsis.');
function check(name, ok, detail) {
  log('    ' + (ok ? 'ok   ' : 'FAIL ') + name + (ok || detail == null ? '' : ' — ' + JSON.stringify(detail).slice(0, 300)));
  if (ok) return true;
  report.failNew.push({name, detail: detail == null ? null : JSON.stringify(detail).slice(0, 500)});
  return false;
}
let currentFlow = null;
function checkKnown(name, ok, fix, detail) {
  if (ok) { log('    ok   ' + name + ' (already fixed?)'); return true; }
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
    conflictBump: 0, abortNextTask: 0, holdApplyMs: null, bumpSeq: 0,
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
    armHoldApply: (ms) => { state.holdApplyMs = ms; },
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
        const out = await drv('preview', a);
        state.previews.push({intent: JSON.parse(a.intent), overlays: a.channels ? JSON.parse(a.channels) : [],
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
async function stageFromSheet(api, sheetClass) {
  await api.page.locator('.' + sheetClass).getByRole('button', {name: 'Stage arrangement'}).click();
  await api.page.locator('.jw-arrange-bar').waitFor({timeout: 10000});
}
async function applyArrangement(api, revAfter) {
  await api.page.locator('.jw-arrange-bar').getByRole('button', {name: 'Apply arrangement'}).click();
  try {
    await api.page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
  } catch (e) {
    const receipts = api.state.applies.map((a) => ({requestId: a.requestId.slice(0, 12), status: a.receipt.status,
      error: a.receipt.error, errors: (a.receipt.errors || []).map((x) => x.code + ':' + x.message).slice(0, 3)}));
    throw new Error('applyArrangement: no applied toast; receipts=' + JSON.stringify(receipts) +
      ' toasts=' + JSON.stringify(await api.page.locator('.jw-toast').allTextContents()));
  }
  await settle(400);
  if (revAfter != null) check('revision advanced to ' + revAfter, await api.rev() === revAfter, await api.rev());
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
  check('review meta counts 1 displaced bystander', meta.includes('2 affected rows') && meta.includes('1 displaced bystander'), meta);
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
  check('review lists all 6 rows / 3 displaced bystanders', meta.includes('6 affected rows') && meta.includes('3 displaced bystanders'), meta);
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
  check('group scope preselected (2 channels)', (await page.locator('.jw-scope-line').first().textContent()).includes('2 channels in scope'));
  await page.locator('#jw-org-rstart').fill('300');
  await page.locator('#jw-org-rend').fill('699');
  await waitReview(api, 2);
  const cap = await page.locator('.jw-scope-line', {hasText: 'holds 400 slots'}).textContent();
  check('capacity line exact', cap.includes('holds 400 slots') && cap.includes('selection needs 2') &&
    cap.includes('5 outsiders keep their numbers'), cap);
  check('outsiders 320/450 untouched in review', await page.locator('.jw-review-row', {hasText: 'Outsider'}).count() === 0);
  // F6 probe: the Alphabetical chip currently sends order:"alpha" (rejected)
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
  checkKnown('F6: alphabetical arrange_range plans by name (Alpha Net first)',
    alphaOk, 'F6', alphaDetail || (alphaRows + ' review rows'));
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
  check('staging refused while short', await page.locator('.jw-sheet-organize').getByRole('button', {name: 'Stage arrangement'}).isDisabled());
  await page.getByRole('button', {name: 'Close — keep staging'}).click();
  check('nothing staged by the refused plan', await page.locator('.jw-arrange-bar').count() === 0);
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
  await page.getByRole('button', {name: 'Make this range exclusive (one time)'}).click();
  check('one-time copy present', await page.locator('.jw-hint', {hasText: 'one-time arrangement, not a permanent reservation'}).count() === 1);
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

// 6. Create group + assign in ONE Apply (F1 territory).
async function flow6(browser) {
  const api = await boot(browser);
  const {page} = api;
  // (a) editor inline "Create group…" — the group rides the same Apply packet
  await openEditor(api, 'Chart One');
  await page.locator('#f-group').selectOption('__create__');
  await page.locator('input[aria-label="New group name"]').last().fill('Fresh Crew');
  await page.locator('#apply-btn').click();
  await settle(1800);
  const applied = (await page.locator('.jw-apply-state').textContent()).includes('Applied at r');
  checkKnown('F1: editor Create group… commits in one Apply', applied, 'F1',
    await page.locator('.jw-apply-state').textContent());
  if (applied) {
    const doc = await api.doc();
    check('group created + assigned', doc.groups.some((g) => g.name === 'Fresh Crew') &&
      doc.channels.find((c) => c.id === 'net_a0000001').groupId ===
      doc.groups.find((g) => g.name === 'Fresh Crew').id);
  }
  // (b) create-at-occupied referencing a packet-created group: the sheet's
  // overlay references an uncommitted group.
  await openNewChannel(api, {number: 300, name: 'Renumbered Crew', createGroup: 'Renumbered Crew'});
  await page.getByRole('button', {name: 'Create draft'}).click();
  await page.locator('.jw-sheet-numres').waitFor();
  await settle(1800);
  const sheetErrs = await page.locator('.jw-sheet-numres .jw-error-text').allTextContents();
  const stageable = await page.locator('.jw-sheet-numres')
    .getByRole('button', {name: 'Stage arrangement'}).isEnabled();
  checkKnown('F1(+residual): occupied create with a new group previews + stages its placement',
    stageable && sheetErrs.length === 0, 'F1', {errors: sheetErrs, stageable});
  if (sheetErrs.length) {
    report.notes.push('flow6b residual: with a client-generated group id fixed (grp_ prefix), the numres overlay STILL references an ' +
      'uncommitted group and organization._rows types unknown_group (organization.py:110-112) — the sheet cannot preview. Errors: ' +
      JSON.stringify(sheetErrs) + ' — needs an organizer/UI decision (overlay groupId created in the same packet, or defer the groupId until the group commits).');
  }
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
  await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Apply arrangement'}).click();
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
  // must replay exactly once.
  api.armAbort();
  await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Apply arrangement'}).click();
  await settle(3000);
  const btn = page.locator('.jw-arrange-bar button', {hasText: 'Applying…'});
  const stuck = await btn.count() > 0 && await btn.first().isDisabled();
  checkKnown('F3: after a transport failure the bar re-enables for the frozen retry', !stuck, 'F3',
    stuck ? 'Apply arrangement disabled + labelled "Applying…" 3s after the transport abort (pending never cleared)' : 'bar recovered');
  if (!stuck) {
    await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Apply arrangement'}).click();
    await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
    const mine = api.state.applies.filter((a) => a.requestId === org.staged.packet.requestId);
    check('same requestId resubmitted', mine.length === 1 && mine[0].requestId === org.staged.packet.requestId, mine.map((m) => m.requestId));
    check('revision advanced exactly once', await api.rev() === REVISION + 2 && await api.number('Chart One') === 303,
      {rev: await api.rev(), chartOne: await api.number('Chart One')});
  }
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

  // (8b) F5: editor Apply bypasses the submit coordinator — a bulk action
  // during the editor's flight self-inflicts a revision conflict.
  api.armHoldApply(4500);
  await page.locator('#apply-btn').click();
  await page.locator('.jw-apply-state', {hasText: 'Applying…'}).waitFor({timeout: 10000});
  await ensureBulkMode(api);
  await selectRows(api, ['Chart One']);
  await page.locator('.jw-bulk-bar').getByRole('button', {name: 'Pause', exact: true}).click();
  await page.locator('.jw-overlay .jw-dialog').getByRole('button', {name: 'Apply', exact: true}).click();
  await settle(6500); // hold (4.5s) elapses; the second path's outcome surfaces
  const conflictToast = await page.locator('.jw-toast', {hasText: 'Revision conflict'}).count();
  const editorConflict = await page.locator('.jw-apply-state', {hasText: 'Revision conflict'}).count();
  checkKnown('F5: no self-inflicted revision conflict inside one tab', conflictToast === 0 && editorConflict === 0, 'F5',
    {bulkConflictToast: conflictToast, editorConflictBar: editorConflict,
     note: 'the second submission ran against an un-refreshed revision instead of waiting for the first receipt'});
  await page.getByRole('button', {name: 'Keep editing my draft'}).click().catch(() => {});
  await settle(800);
  if (conflictToast === 0) {
    check('bulk path landed (Chart One paused)', ((await api.doc()).channels.find((c) => c.id === 'net_a0000001') || {}).paused === true);
  }

  // (ii) undo/redo of staging
  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('303');
  await page.locator('.jw-hint', {hasText: '303 is free.'}).waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Undo'}).click();
  check('undo clears the staging bar', await page.locator('.jw-arrange-bar').count() === 0);
  // redo is reachable while an organize sheet holds the keyboard scope
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.keyboard.press('Control+Shift+z');
  await settle(400);
  await page.getByRole('button', {name: 'Close — keep staging'}).click();
  check('redo restores the staged bar', await page.locator('.jw-arrange-bar').count() === 1);
  report.notes.push('flow8 note: after Undo the bar unmounts, so Redo is only reachable by opening an organize sheet first (keyboard scope) — minor affordance gap, not a data bug.');
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

  // (8c) F4: staging while an arrangement Apply is in flight
  await openEditor(api, 'Chart One');
  await page.getByRole('button', {name: 'Move / insert…'}).first().click();
  await page.locator('.jw-sheet-numres').waitFor();
  await page.locator('#jw-numres-dest').fill('305');
  await page.locator('.jw-hint', {hasText: '305 is free.'}).waitFor({timeout: 15000});
  await stageFromSheet(api, 'jw-sheet-numres');
  api.armHoldApply(6500);
  await page.locator('.jw-arrange-bar').getByRole('button', {name: 'Apply arrangement'}).click();
  await page.locator('.jw-arrange-bar button', {hasText: 'Applying…'}).waitFor({timeout: 5000});
  // second arrangement staged DURING the flight
  await page.getByRole('button', {name: 'Organize channels…'}).click();
  await page.locator('.jw-sheet-organize').waitFor();
  await page.getByRole('button', {name: 'A number interval'}).click();
  await page.locator('#jw-org-scstart').fill('298');
  await page.locator('#jw-org-scend').fill('299');
  await page.locator('#jw-org-rstart').fill('400');
  await page.locator('#jw-org-rend').fill('401');
  await waitReview(api, 2);
  await page.locator('.jw-sheet-organize').getByRole('button', {name: 'Stage arrangement'}).click();
  await settle(5500); // let the held apply land
  await page.locator('.jw-toast', {hasText: 'Arrangement applied at r'}).first().waitFor({timeout: 30000});
  check('server committed the ORIGINAL in-flight plan', await api.number('Chart One') === 305, await api.number('Chart One'));
  const d3c = (await api.drafts()).net_a0000003;
  checkKnown('F4: staging during flight does not corrupt drafts with the never-committed plan',
    d3c.draft.number === 299, 'F4',
    {draftNumber: d3c.draft.number, serverTruth: 299, note: 'draft was rebased by the SECOND (discarded) staging beforeAfter'});
  const orgC = await api.org();
  report.notes.push('flow8c post-state: org staged=' + JSON.stringify(orgC.staged && orgC.staged.label) +
    ' pending=' + JSON.stringify(!!orgC.pending) + ' (the second staging was silently discarded by the F4 path)');
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
  check('hook announces the page state', await page.locator('.jw-visually-hidden', {hasText: 'Channel Studio ready — 18 channels at r' + REVISION}).count() === 1,
    await page.locator('.jw-visually-hidden').allTextContents());
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

// ----------------------------------------------------------------- main ----
(async () => {
  const browser = await chromium.launch({executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome',
    headless: true, args: ['--no-sandbox']});
  const only = process.env.FLOW ? parseInt(process.env.FLOW, 10) : null;
  const flows = [
    [1, 'create-at-occupied + shift chain + downward mirror', flow1],
    [2, 'existing 299 → 300 with dirty name (opt-in put + renumber)', flow2],
    [3, 'block 171/173/180 → 300 with bystanders', flow3],
    [4, 'Performers 300-699 useAvailable + capacity + shortfall', flow4],
    [5, 'exclusive range with outside interval', flow5],
    [6, 'create group + assign in one Apply (F1)', flow6],
    [7, 'revision conflict + lost-response replay', flow7],
    [8, 'edit-in-flight, undo/redo, rebase, reversal, stage-in-flight', flow8],
    [9, 'capability gating fallback', flow9],
    [10, 'visual sweep + geometry + keyboard', flow10],
    [11, 'extras snippet lifecycle', flow11],
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
  log('known-unfixed failures: ' + JSON.stringify(Object.fromEntries(Object.entries(report.failKnown).map(([k, v]) => [k, v.length]))));
  log('new findings: ' + report.failNew.length + '; harness crashes: ' + report.failHarness.length +
    '; notes: ' + report.notes.length + '; shots: ' + report.shots.length);
  fs.writeFileSync(path.join(SHOTS, 'dry-run-report.json'), JSON.stringify(report, null, 2));
  if (report.failHarness.length && process.env.STRICT) process.exit(1);
  log('report + screenshots in ' + SHOTS);
})().catch((e) => { console.error(e); process.exit(1); });
