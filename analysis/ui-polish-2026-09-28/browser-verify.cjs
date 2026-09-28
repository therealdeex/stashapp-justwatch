// Verification harness for the 2026-09-28 UI polish pass (chip management
// overhaul + play-order chips + visual modernization). Serves the REAL
// ui/index.js + ui/styles.css in headless Chromium against a mock GraphQL
// backend; asserts the new facet chip ergonomics (cap → "+N more" → manage
// mode, filter, select-all-shown, Remove-N confirm thresholds, Clear-all both
// lists), the play-order chips round-trip (sort on the wire, emptied facet
// keys omitted), the topbar More menu, the controlled groups rename, the
// preview Refresh button — then captures a dark+light theme screenshot sweep.
//
//   node browser-verify.cjs [outDir]
// Deps (env overrides): JW_AUDIT_PLAYWRIGHT, JW_AUDIT_REACT_ROOT, JW_AUDIT_CHROME.
const fs = require('fs');
const path = require('path');
const {chromium} = require(process.env.JW_AUDIT_PLAYWRIGHT || '/tmp/stash-verify/node_modules/playwright-core');
const root = path.resolve(__dirname, '../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';
const outDir = process.argv[2] || path.join(__dirname, 'shots');
fs.mkdirSync(outDir, {recursive: true});

const clone = (x) => JSON.parse(JSON.stringify(x));

// ---- fixture: one channel with 120 include tags + 8 exclusion tags ---------
const TAGS = {};
for (let i = 1; i <= 130; i++) TAGS[i] = 'Tag ' + i;
for (let i = 301; i <= 308; i++) TAGS[i] = 'Ex Tag ' + i;
const BIG_SOURCE = {
  type: 'criteria',
  tags: Array.from({length: 120}, (_, i) => String(i + 1)),
  excludeTags: Array.from({length: 8}, (_, i) => String(301 + i)),
  duration: {min: 600},
  q: '',
};
const FIXTURE = {
  libraryId: 'lib_polish', revision: 3,
  groups: [{id: 'grp_my', name: 'My Channels', position: 1}],
  channels: [
    {id: 'ch_00000001', kind: 'ch', number: 1, name: 'Big Tag Channel', glyph: null,
      color: '#3949AB', groupId: 'grp_my', sort: 'shuffle', seed: 1, enabled: true,
      archived: false, paused: false, source: clone(BIG_SOURCE), sourceLabel: 'Rules',
      programming: {mode: 'fixed'}, provenance: {origin: 'custom'}},
    {id: 'ch_00000002', kind: 'ch', number: 2, name: 'Quiet Channel', glyph: null,
      color: '#00897B', groupId: 'grp_my', sort: 'newest', seed: 2, enabled: true,
      archived: false, paused: false, source: {type: 'criteria'}, sourceLabel: 'Rules',
      programming: {mode: 'fixed'}, provenance: {origin: 'custom'}},
  ],
};
let doc;

const FAS = {
  faTags: {iconName: 'tags', icon: [512, 512, [], 'f02c']},
  faUser: {iconName: 'user', icon: [448, 512, [], 'f007']},
  faBuilding: {iconName: 'building', icon: [384, 512, [], 'f1ad']},
  faTrashAlt: {iconName: 'trash-alt', icon: [448, 512, [], 'f2ed']},
};

function makePage(theme) {
  const scripts = {
    '/react.js': fs.readFileSync(path.join(deps, 'react/umd/react.development.js')),
    '/react-dom.js': fs.readFileSync(path.join(deps, 'react-dom/umd/react-dom.development.js')),
    '/ui.js': fs.readFileSync(path.join(root, 'ui/index.js')),
    '/ui.css': fs.readFileSync(path.join(root, 'ui/styles.css')),
  };
  const vars = theme === 'light'
    ? '--body-color:#f8f9fa;--text-color:#212529;--primary:#0d6efd;--danger:#dc3545;'
    : '--body-color:#202b33;--text-color:#f0f0f5;--primary:#6caddf;--danger:#d9534f;';
  const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/ui.css">
<style>:root{${vars}}</style>
<body style="background:var(--body-color);margin:0"><div id="root"></div>
<script src="/react.js"></script><script src="/react-dom.js"></script>
<script>
window.PluginApi = {React, libraries:{FontAwesomeSolid: ${JSON.stringify(FAS)}},
  components:{Icon: function Icon(p){ return React.createElement('span', {className:p.className, style:p.style,
    'data-icon': (p.icon && p.icon.iconName) || ''}, (p.icon && p.icon.iconName) || ''); }},
  register:{route(p,c){window.AuditApp=c}}};
</script>
<script src="/ui.js"></script>
<script>ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(window.AuditApp));</script>`;
  return {scripts, html};
}

async function boot(browser, theme) {
  doc = clone(FIXTURE); // every boot starts from a pristine library
  const {scripts, html} = makePage(theme || 'dark');
  const context = await browser.newContext({viewport: {width: 1500, height: 950}});
  const page = await context.newPage();
  const state = {errors: [], tasks: [], previews: 0, receipts: {}};
  page.on('pageerror', (e) => state.errors.push(e.message));
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/graphql') {
      const body = route.request().postDataJSON(), vars = body.variables || {};
      let data = {};
      if (body.query.includes('runPluginTask')) {
        const a = vars.a;
        state.tasks.push(clone(a));
        const ops = JSON.parse(a.ops);
        for (const op of ops) {
          if (op.op === 'channel.put') {
            doc.channels = doc.channels.map((c) => (c.id === op.channel.id ? Object.assign(clone(op.channel), {id: c.id, seed: c.seed}) : c));
          }
        }
        doc.revision++;
        state.receipts[a.requestId] = {requestId: a.requestId, status: 'committed', revision: doc.revision, refresh: {}};
        data = {runPluginTask: 'job-1'};
      } else if (body.query.includes('runPluginOperation')) {
        const a = vars.args || {};
        let out;
        switch (a.mode) {
          case 'GetChannelLibrary': out = Object.assign(clone(doc), {total: doc.channels.length}); break;
          case 'GetChannelDefinition': {
            const cch = doc.channels.find((c) => c.id === a.channelId);
            out = {revision: doc.revision, channel: cch ? clone(cch) : null, summary: []}; break;
          }
          case 'PreviewChannelPool':
            state.previews++;
            out = {status: 'ok', poolCount: 128, rotationSize: 50, rotationComplete: true, sample: []}; break;
          case 'ValidateChannelChanges': out = {valid: true, errors: [], effects: [], revision: doc.revision}; break;
          case 'GetChannelApplyResult': out = state.receipts[a.requestId] || {status: 'unknown'}; break;
          case 'GetChannelRefreshStatus': out = {libraryRevision: doc.revision, snapshotRevision: doc.revision,
            computedAt: null, channelId: a.channelId, pending: null, health: {healthStatus: 'ok', sceneCount: 12}}; break;
          default: out = {};
        }
        data = {runPluginOperation: out};
      } else if (body.query.includes('findTags')) {
        const q = (vars.f && vars.f.q) || '';
        data = {findTags: {tags: Object.entries(TAGS).filter(([id, n]) => !q || n.toLowerCase().includes(q))
          .slice(0, 100).map(([id, name]) => ({id, name, scene_count: Number(id) * 7 % 1300}))}};
      } else if (body.query.includes('findTag(')) {
        data = {findTag: {name: TAGS[vars.id] || null}};
      } else if (body.query.includes('findPerformer(')) {
        data = {findPerformer: {name: 'Performer ' + vars.id}};
      } else if (body.query.includes('findStudio(')) {
        data = {findStudio: {name: 'Studio ' + vars.id}};
      }
      return route.fulfill({contentType: 'application/json', body: JSON.stringify({data})});
    }
    if (scripts[url.pathname]) {
      return route.fulfill({contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: scripts[url.pathname]});
    }
    return route.fulfill({contentType: 'text/html', body: html});
  });
  await page.goto('http://polish.invalid/');
  await page.locator('#f-name').waitFor({timeout: 10000});
  return {context, page, state};
}

const failures = [];
function check(name, ok, detail) {
  console.log((ok ? '  ✓ ' : '  ✗ ') + name + (ok ? '' : (detail != null ? ' — ' + JSON.stringify(detail) : '')));
  if (!ok) failures.push(name);
  return ok;
}
const settle = (ms) => new Promise((r) => setTimeout(r, ms));

// The tags facet row is the first .jw-rule-row in the rules card.
const tagsRow = (page) => page.locator('.jw-rule-row', {hasText: 'Tags'}).first();

(async () => {
  const browser = await chromium.launch({executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome',
    headless: true, args: ['--no-sandbox']});

  // ================= assertions (dark theme boot) =================
  {
    const b = await boot(browser, 'dark');
    const {page, state} = b;
    // wait for tag-name resolution of the visible chips
    await page.locator('.jw-entity-chip', {hasText: 'Tag 1'}).first().waitFor({timeout: 10000});
    await settle(400);

    // --- A: cap + overflow button ---
    const row = tagsRow(page);
    const chipCount = await row.locator('.jw-entity-summary').first().locator('.jw-entity-chip:not(.jw-entity-overflow)').count();
    check('cap renders 100 chips', chipCount === 100, chipCount);
    const overflow = row.locator('button.jw-entity-overflow').first();
    check('overflow chip is a button with the count', await overflow.count() === 1
      && (await overflow.textContent()).includes('20'), await overflow.textContent().catch(() => null));

    // --- A: overflow -> manage mode, focus in the filter ---
    await overflow.click();
    await settle(200);
    check('manage mode: chips become toggle buttons',
      await row.locator('.jw-entity-chip-check[aria-pressed]').count() === 100);
    check('overflow becomes a hint in manage mode',
      await row.locator('.jw-entity-summary .jw-hint', {hasText: 'more hidden'}).count() >= 1);
    check('focus moved into the filter', await page.evaluate(
      () => document.activeElement && document.activeElement.classList.contains('jw-chip-filter')));

    // --- A: filter narrows (Tag 1 -> ids 1,10-19 = 11 matches) ---
    await row.locator('.jw-chip-filter').fill('tag 1');
    await settle(250);
    check('filter narrows to 11', await row.locator('.jw-entity-chip-check').count() === 11,
      await row.locator('.jw-entity-chip-check').count());

    // --- A: select all shown / deselect one / Remove with confirm (>10) ---
    await row.getByRole('button', {name: 'Select all shown'}).click();
    check('select all shown selects 11', await row.locator('.jw-entity-chip-check.jw-chip-active').count() === 11);
    await row.locator('.jw-entity-chip-check', {hasText: 'Tag 19'}).click();
    check('deselect drops to 10', await row.locator('.jw-entity-chip-check.jw-chip-active').count() === 10);
    await row.locator('.jw-entity-chip-check', {hasText: 'Tag 19'}).click(); // reselect -> 11
    await row.getByRole('button', {name: /Remove 11 selected/}).click();
    await settle(250);
    check('Remove 11 asks first', await page.locator('.jw-overlay .jw-dialog').count() === 1);
    await page.locator('.jw-dialog').getByRole('button', {name: 'Remove 11'}).click();
    await settle(350);
    // (the filter persists; newly-resolved names like "Tag 100-120" re-match
    // "tag 1", so assert removal via a name only the removed set contained)
    await row.locator('.jw-chip-filter').fill('tag 19');
    await settle(250);
    check('11 chips removed (Tag 19 gone)', await row.locator('.jw-entity-chip-check').count() === 0,
      await row.locator('.jw-entity-chip-check').allTextContents());

    // --- A: small remove (<=10) skips the confirm ---
    await row.locator('.jw-chip-filter').fill('tag 2'); // ids 2,20-29 = 11 shown
    await settle(250);
    check('filter tag 2 shows 11', await row.locator('.jw-entity-chip-check').count() === 11,
      await row.locator('.jw-entity-chip-check').count());
    await row.getByRole('button', {name: 'Tag 20', exact: true}).click();
    await row.getByRole('button', {name: 'Tag 2', exact: true}).click();
    await row.getByRole('button', {name: 'Remove 2 selected'}).click();
    await settle(300);
    check('Remove 2 does NOT ask', await page.locator('.jw-overlay').count() === 0);
    check('2 chips removed (9 of the tag-2 family left)',
      await row.locator('.jw-entity-chip-check').count() === 9,
      await row.locator('.jw-entity-chip-check').allTextContents());

    // --- A: Escape exits manage mode (focus is back in the filter) ---
    check('focus returned to the filter after removal', await page.evaluate(
      () => document.activeElement && document.activeElement.classList.contains('jw-chip-filter')));
    await page.keyboard.press('Escape');
    await settle(200);
    check('Escape exits manage mode', await row.locator('.jw-entity-chip-check').count() === 0
      && await row.locator('.jw-entity-chip').count() > 0);

    // --- A: Clear all exclusions (8 <= 10: no confirm) ---
    const exclBlock = row.locator('.jw-facet-chips').nth(1);
    await exclBlock.getByRole('button', {name: 'Clear all'}).click();
    await settle(300);
    check('clear exclusions skips confirm (8 <= 10)', await page.locator('.jw-overlay').count() === 0);
    check('exclusion chips gone', await exclBlock.locator('.jw-entity-chip').count() === 0);

    // --- A: Clear all includes (107 > 10: confirm) ---
    const inclBlock = row.locator('.jw-facet-chips').first();
    await inclBlock.getByRole('button', {name: 'Clear all'}).click();
    await settle(250);
    check('clear includes asks first (107 > 10)', await page.locator('.jw-overlay .jw-dialog').count() === 1);
    await page.locator('.jw-dialog').getByRole('button', {name: 'Clear all'}).click();
    await settle(350);
    check('facet row back to the empty state',
      await row.locator('.jw-hint', {hasText: 'none — every tag matches'}).count() === 1);
    check('ALL/ANY disabled with no ids',
      await row.locator('.jw-logic button[disabled]').count() === 2);

    // --- C1: play-order chips ---
    const progCard = page.locator('.jw-card', {hasText: 'Play order'});
    await progCard.getByRole('button', {name: 'Newest'}).click();
    check('Newest chip activates', await progCard.getByRole('button', {name: 'Newest'})
      .evaluate((el) => el.classList.contains('jw-chip-active')));
    check('dirty pill appears', await page.locator('.jw-pill-dirty').count() >= 1);

    // --- Apply: sort rides the wire; emptied facet keys omitted ---
    await page.locator('#f-name').press('Control+Enter');
    await page.waitForSelector('text=Applied at r', {timeout: 15000});
    const put = state.tasks.length
      && JSON.parse(state.tasks[state.tasks.length - 1].ops).find((op) => op.op === 'channel.put');
    check('apply submitted a channel.put', !!put);
    if (put) {
      check('sort=newest on the wire', put.channel.sort === 'newest', put.channel.sort);
      check('emptied facet keys omitted on the wire',
        !('tags' in put.channel.source) && !('tagsAny' in put.channel.source) && !('excludeTags' in put.channel.source),
        Object.keys(put.channel.source));
    }

    // --- C3: preview Refresh re-queries ---
    const before = state.previews;
    await page.locator('.jw-card', {hasText: 'Preview'}).getByRole('button', {name: 'Refresh'}).click();
    await settle(600);
    check('Refresh re-runs the preview', state.previews > before, {before, after: state.previews});

    // --- C6: topbar More menu ---
    await page.getByRole('button', {name: 'More actions'}).click();
    for (const label of ['Groups…', 'Export CSV', 'Download diagnostics', 'Reload library']) {
      check('menu has ' + label, await page.getByRole('menuitem', {name: label}).count() === 1);
    }
    await page.getByRole('menuitem', {name: 'Groups…'}).click();
    await page.locator('.jw-groups-row').first().waitFor();

    // --- C5: controlled rename dirties without blur ---
    const nameInput = page.locator('.jw-groups-row .jw-groups-name').first();
    await nameInput.click();
    await nameInput.pressSequentially('2', {delay: 30});
    await settle(250);
    const applyBtn = page.locator('.jw-dialog-foot').getByRole('button', {name: 'Apply'});
    check('rename without blur enables Apply', await applyBtn.isEnabled());
    await page.keyboard.press('Escape');

    check('no page errors', state.errors.length === 0, state.errors);
    await b.context.close();
  }

  // ================= screenshot sweep: dark + light =================
  for (const theme of ['dark', 'light']) {
    const b = await boot(browser, theme);
    const {page} = b;
    await page.locator('.jw-entity-chip', {hasText: 'Tag 1'}).first().waitFor({timeout: 10000});
    await settle(600);
    const shot = (name) => page.screenshot({path: path.join(outDir, theme + '-' + name + '.png')})
      .then(() => console.log('  📸 ' + theme + '-' + name));

    await shot('01-main');
    const row = tagsRow(page);
    await row.scrollIntoViewIfNeeded();
    await settle(250);
    await shot('02-rules');

    // manage mode over the full 120
    await row.locator('button.jw-entity-overflow').first().click();
    await settle(300);
    await shot('03-manage');
    await page.keyboard.press('Escape');
    await settle(150);

    // ANY/ALL toast + programming play-order chips + preview
    await row.locator('.jw-logic button', {hasText: 'ANY'}).click();
    await settle(300);
    const prog = page.locator('.jw-card', {hasText: 'Play order'});
    await prog.scrollIntoViewIfNeeded();
    await prog.getByRole('button', {name: 'Top rated'}).click();
    await settle(500);
    await shot('04-programming-toast');

    // a dialog + the More menu
    await page.getByRole('button', {name: 'More actions'}).click();
    await settle(200);
    await shot('05-more-menu');
    await page.getByRole('menuitem', {name: 'Groups…'}).click();
    await page.locator('.jw-groups-row').first().waitFor();
    await settle(250);
    await shot('06-groups');
    await page.keyboard.press('Escape');

    // new channel dialog (styled band chips)
    await page.getByRole('button', {name: '+ New channel…'}).click();
    await settle(300);
    await shot('07-new-channel');
    await page.keyboard.press('Escape');

    await b.context.close();
  }

  await browser.close();
  if (failures.length) {
    console.error('\nFAILURES: ' + failures.join(' | '));
    process.exit(1);
  }
  console.log('\nall checks passed; screenshots in ' + outDir);
})().catch((e) => { console.error(e); process.exit(1); });
