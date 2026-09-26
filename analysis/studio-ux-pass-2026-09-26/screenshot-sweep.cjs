// Screenshot harness for the Channel Studio UI (2026-09-26 UX pass
// verification). Serves the REAL ui/index.js + ui/styles.css in headless
// Chromium with a mocked GraphQL (rich fixture: groups, bands, rules,
// preview, history, health states) and captures a before/after PNG sweep of
// every surface (main, rules, dirty, applied, away-toast, picker, groups
// incl. staged delete, new channel, history, bulk, search states, collapsed
// groups, off-air pill, narrow viewport). Also exercises the Ctrl/Cmd+Enter
// Apply shortcut and prints the away-apply toast it produces.
//
//   node screenshot-sweep.cjs <outDir> [uiDir]
//   UI_DIR defaults to the repo ui/. Requires playwright-core + React UMD at
//   the audit locations (JW_AUDIT_REACT_ROOT, JW_AUDIT_CHROME env overrides).
const fs = require('fs');
const path = require('path');
const {chromium} = require('/tmp/stash-verify/node_modules/playwright-core');

const root = path.resolve(__dirname, '../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';
const uiDir = process.argv[3] || path.join(root, 'ui');
const outDir = process.argv[2] || '/tmp/jw-shots/out';
fs.mkdirSync(outDir, {recursive: true});

const clone = (x) => JSON.parse(JSON.stringify(x));

// ---- fixture --------------------------------------------------------------
const TAGS = {}, PERF = {}, STUD = {};
for (let i = 1; i <= 60; i++) TAGS[i] = ['Blender', 'Cosplay', 'Interview', 'ASMR', 'Amateur', '4K', 'Vintage', 'Podcast', 'Live', 'Tutorial'][i % 10] + ' ' + i;
for (let i = 1; i <= 40; i++) PERF[i] = 'Performer ' + i;
for (let i = 1; i <= 30; i++) STUD[i] = 'Studio ' + i;

function ch(id, number, name, groupId, extra) {
  return Object.assign({
    id, kind: number < 100 ? 'ch' : 'net', number, name,
    glyph: null, color: ['#E91E63', '#3949AB', '#00897B', '#F57C00'][number % 4],
    groupId, sort: 'shuffle', seed: number, enabled: true,
    archived: false, paused: false,
    source: {type: 'criteria'}, sourceLabel: '', programming: {mode: 'fixed'},
    provenance: {origin: 'custom'},
  }, extra || {});
}

const richSource = {
  type: 'criteria',
  tags: ['5', '7'], tagsAny: [], excludeTags: ['9'],
  performers: ['11'], performersAny: [], excludePerformers: [],
  studios: [], studiosAny: ['22'], excludeStudios: ['3'],
  date: {from: '2020-01-01', to: ''},
  duration: {min: 3600},
  createdAt: {withinDays: 30},
  studioSceneCount: {min: 2},
  q: 'director’s cut',
};

const doc = {
  libraryId: 'lib_shots', revision: 7,
  groups: [
    {id: 'grp_my', name: 'My Channels', position: 1},
    {id: 'grp_net', name: 'Networks', position: 2},
    {id: 'grp_arc', name: 'Archive', position: 3},
  ],
  channels: [
    ch('ch_00000001', 1, 'Favorites', 'grp_my', {glyph: '', color: '#E91E63', source: clone(richSource), sourceLabel: 'Rules'}),
    ch('ch_00000002', 2, 'Weekend Clips', 'grp_my', {}),
    ch('ch_00000100', 100, 'Retro Rewind', 'grp_net', {source: {type: 'tag', ids: ['5', '9']}, sourceLabel: 'Tag'}),
    ch('ch_00000101', 101, 'Midnight Movies', 'grp_net', {paused: true}),
    ch('ch_00000102', 102, 'Studio Spotlight', 'grp_net', {source: {type: 'studio', id: '22'}, sourceLabel: 'Studio'}),
    ch('ch_00000103', 103, 'Star Channel', 'grp_net', {source: {type: 'performer', id: '11'}, sourceLabel: 'Performer'}),
    ch('ch_00000104', 104, 'Saved Picks', 'grp_net', {source: {type: 'savedFilter', id: '42'}, sourceLabel: 'Saved search'}),
    ch('ch_00000105', 105, 'Off Air Radio', 'grp_net', {}),
    ch('ch_00000106', 106, 'Neon Nights', 'grp_net', {glyph: '', color: '#5E35B1'}),
    ch('ch_00000107', 107, 'Golden Hour', 'grp_net', {}),
    ch('ch_00000108', 108, 'Deep Cuts', 'grp_net', {}),
    ch('ch_00000109', 109, 'The Vault', 'grp_arc', {archived: true}),
    ch('ch_00000110', 110, 'Old Tube', 'grp_arc', {archived: true}),
  ],
};
const receipts = {};
const health = {
  ch_00000105: {pending: null, health: {healthStatus: 'offAir', sceneCount: 0}},
  ch_00000101: {pending: {enqueuedAt: new Date().toISOString()}, health: {healthStatus: 'ok', sceneCount: 4}},
};
const historyEntries = [11, 9, 7].map((rev, i) => ({
  revision: rev,
  savedAt: new Date(Date.now() - (i + 1) * 86400000 * 3).toISOString(),
  channelCount: doc.channels.length,
  summary: 'edit',
  channels: [{id: 'ch_00000001', kind: 'ch', number: 1, name: 'Favorites r' + rev, glyph: null, color: '#E91E63',
    groupId: 'grp_my', sort: 'shuffle', seed: 1, enabled: true, archived: false, paused: false,
    source: clone(richSource), sourceLabel: 'Rules', programming: {mode: 'fixed'}, provenance: {origin: 'custom'}}],
}));

const SAMPLE = ['Cold Open', 'Neon District', 'After Hours', 'The Long Cut', 'Static Dreams', 'Rewind 1984']
  .map((title, i) => ({id: 'sc_' + (1000 + i), title, studio: STUD[22 + ''] || 'Studio 22',
    duration: 2100 + i * 660, date: '202' + (i % 5) + '-0' + (i + 1) + '-1' + i, preview: null}));

// A tiny fake FontAwesome: real unicode codepoints from GLYPH_POOL for a few
// glyphs, a couple of UI icons, and an Icon component that renders the key
// name (proves resolution + placement; real Stash renders SVG paths).
const FAS = {
  faHeart: {iconName: 'heart', icon: [512, 512, [], 'f004']},
  faMusic: {iconName: 'music', icon: [512, 512, [], 'f001']},
  faTags: {iconName: 'tags', icon: [512, 512, [], 'f02c']},
  faUser: {iconName: 'user', icon: [448, 512, [], 'f007']},
  faBuilding: {iconName: 'building', icon: [384, 512, [], 'f1ad']},
  faTrashAlt: {iconName: 'trash-alt', icon: [448, 512, [], 'f2ed']},
  faTv: {iconName: 'tv', icon: [640, 512, [], 'f26c']},
  faCheck: {iconName: 'check', icon: [512, 512, [], 'f00c']},
};

function makePage(state) {
  const scripts = {
    '/react.js': fs.readFileSync(path.join(deps, 'react/umd/react.development.js')),
    '/react-dom.js': fs.readFileSync(path.join(deps, 'react-dom/umd/react-dom.development.js')),
    '/ui.js': fs.readFileSync(path.join(uiDir, 'index.js')),
    '/ui.css': fs.readFileSync(path.join(uiDir, 'styles.css')),
  };
  const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/ui.css">
<body style="background:#202b33;margin:0"><div id="root"></div>
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

async function boot(browser, viewport) {
  const {scripts, html} = makePage();
  const context = await browser.newContext({viewport: viewport || {width: 1500, height: 950}});
  const page = await context.newPage();
  const state = {errors: []};
  page.on('pageerror', (e) => state.errors.push(e.message));
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/graphql') {
      const body = route.request().postDataJSON(), vars = body.variables || {};
      let data = {};
      if (body.query.includes('runPluginTask')) {
        const a = vars.a;
        const ops = JSON.parse(a.ops);
        for (const op of ops) {
          if (op.op === 'channel.put') doc.channels = doc.channels.map((c) => (c.id === op.channel.id ? Object.assign(clone(op.channel), {id: c.id, seed: c.seed}) : c));
          if (op.op === 'channel.create') {
            const id = 'ch_' + String(900 + doc.channels.length);
            receipts.idMap = receipts.idMap || {};
            receipts.idMap[op.tempId] = id;
            doc.channels.push(Object.assign(clone(op.channel), {id}));
          }
        }
        doc.revision++;
        receipts[a.requestId] = {requestId: a.requestId, status: 'committed', revision: doc.revision,
          idMap: receipts.idMap, refresh: {}};
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
            out = a && JSON.stringify(a).includes('ch_00000105')
              ? {status: 'ok', poolCount: 0, rotationSize: 0, rotationComplete: true, sample: []}
              : {status: 'ok', poolCount: 128, rotationSize: 50, rotationComplete: true, sample: clone(SAMPLE)}; break;
          case 'ValidateChannelChanges': out = {valid: true, errors: [], effects: [], revision: doc.revision}; break;
          case 'GetChannelApplyResult': out = receipts[a.requestId] || {status: 'unknown'}; break;
          case 'GetChannelRefreshStatus': out = Object.assign({libraryRevision: doc.revision, snapshotRevision: doc.revision,
            computedAt: null, channelId: a.channelId}, health[a.channelId] || {pending: null, health: {healthStatus: 'ok', sceneCount: 12}}); break;
          case 'GetChannelHistory': out = {entries: clone(historyEntries), total: historyEntries.length}; break;
          default: out = {};
        }
        data = {runPluginOperation: out};
      } else if (body.query.includes('findTags')) {
        const q = (vars.f && vars.f.q) || '';
        data = {findTags: {tags: Object.entries(TAGS).filter(([id, n]) => !q || n.toLowerCase().includes(q))
          .slice(0, 100).map(([id, name]) => ({id, name, scene_count: Number(id) * 7 % 1300}))}};
      } else if (body.query.includes('findPerformers')) {
        const q = (vars.f && vars.f.q) || '';
        data = {findPerformers: {performers: Object.entries(PERF).filter(([id, n]) => !q || n.toLowerCase().includes(q))
          .slice(0, 100).map(([id, name]) => ({id, name, scene_count: Number(id) * 13 % 900}))}};
      } else if (body.query.includes('findStudios')) {
        const q = (vars.f && vars.f.q) || '';
        data = {findStudios: {studios: Object.entries(STUD).filter(([id, n]) => !q || n.toLowerCase().includes(q))
          .slice(0, 100).map(([id, name]) => ({id, name, scene_count: Number(id) * 17 % 700}))}};
      } else if (body.query.includes('findTag(')) {
        data = {findTag: {name: TAGS[vars.id] || null}};
      } else if (body.query.includes('findPerformer(')) {
        data = {findPerformer: {name: PERF[vars.id] || null}};
      } else if (body.query.includes('findStudio(')) {
        data = {findStudio: {name: STUD[vars.id] || null}};
      }
      return route.fulfill({contentType: 'application/json', body: JSON.stringify({data})});
    }
    if (scripts[url.pathname]) {
      return route.fulfill({contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: scripts[url.pathname]});
    }
    return route.fulfill({contentType: 'text/html', body: html});
  });
  await page.goto('http://shots.invalid/');
  return {context, page, state};
}

const settle = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome',
    headless: true, args: ['--no-sandbox']});
  const shot = async (page, name) => { await page.screenshot({path: path.join(outDir, name + '.png')}); console.log('  ✓ ' + name); };

  // --- main studio view -----------------------------------------------------
  {
    const b = await boot(browser);
    await b.page.locator('#f-name').waitFor({timeout: 10000});
    await b.page.waitForTimeout(900); // preview + name resolution settle
    await shot(b.page, '01-main');
    // rules card
    await b.page.locator('.jw-card-title', {hasText: 'What airs (rules)'}).scrollIntoViewIfNeeded();
    await b.page.waitForTimeout(300);
    await shot(b.page, '02-rules');
    // programming + preview + action bar
    await b.page.locator('.jw-card-title', {hasText: 'Preview'}).scrollIntoViewIfNeeded();
    await b.page.waitForTimeout(300);
    await shot(b.page, '03-preview-bar');
    // dirty state
    await b.page.locator('#f-name').fill('Favorites (curated)');
    await b.page.waitForTimeout(500);
    await shot(b.page, '04-dirty');
    // apply via the Ctrl+Enter shortcut -> receipt -> applied bar
    await b.page.locator('#f-name').press('Control+Enter');
    await b.page.waitForSelector('text=Applied at r', {timeout: 15000});
    await shot(b.page, '05-applied-toast');
    await b.context.close();
  }
  // --- away-apply: shortcut commits; switching channels lets the toast land ---
  {
    const b = await boot(browser);
    await b.page.locator('#f-name').waitFor({timeout: 10000});
    await b.page.waitForTimeout(700);
    await b.page.locator('#f-name').fill('Shortcut Renamed');
    await b.page.locator('#f-name').press('Control+Enter');
    await b.page.waitForTimeout(400);
    await b.page.getByRole('option', {name: /Weekend Clips/}).first().click();
    await b.page.waitForSelector('.jw-toast', {timeout: 15000});
    await b.page.waitForTimeout(250);
    await shot(b.page, '05b-away-toast');
    console.log('  away toast text:', JSON.stringify(await b.page.evaluate(() => {
      const t = document.querySelector('.jw-toast'); return t ? t.textContent : null;
    })));
    await b.context.close();
  }
  // --- dialogs ---------------------------------------------------------------
  {
    const b = await boot(browser);
    await b.page.locator('#f-name').waitFor({timeout: 10000});
    await b.page.waitForTimeout(700);
    // entity picker
    await b.page.locator('.jw-rule-row').first().getByRole('button', {name: /Choose/}).click();
    await b.page.locator('.jw-pick-row').first().waitFor();
    await b.page.waitForTimeout(400);
    await b.page.locator('.jw-pick-row').nth(2).click();
    await shot(b.page, '06-picker');
    await b.page.keyboard.press('Escape');
    // groups
    await b.page.getByRole('button', {name: 'Groups…', exact: true}).click();
    await b.page.locator('.jw-groups-row').first().waitFor();
    await shot(b.page, '07-groups');
    await b.page.getByRole('button', {name: 'Delete group Archive'}).click();
    await b.page.waitForTimeout(250);
    await shot(b.page, '07b-groups-deleting');
    await b.page.keyboard.press('Escape');
    // new channel
    await b.page.getByRole('button', {name: '+ New channel…'}).click();
    await shot(b.page, '08-new-channel');
    await b.page.keyboard.press('Escape');
    // history via the ⋯ menu
    await b.page.getByRole('button', {name: 'Channel actions'}).click();
    await b.page.getByRole('menuitem', {name: 'History…'}).click();
    await b.page.locator('.jw-history-row').first().waitFor();
    await shot(b.page, '09-history');
    await b.context.close();
  }
  // --- bulk mode + off-air pill + narrow -------------------------------------
  {
    const b = await boot(browser);
    await b.page.locator('#f-name').waitFor({timeout: 10000});
    await b.page.getByRole('button', {name: 'Select…'}).click();
    await b.page.locator('.jw-dial-row').nth(3).locator('input[type=checkbox]').check();
    await b.page.locator('.jw-dial-row').nth(4).locator('input[type=checkbox]').check();
    await shot(b.page, '10-bulk');
    await b.context.close();
  }
  // --- search: count line, empty-state clear, collapsed group chevron --------
  {
    const b = await boot(browser);
    await b.page.locator('#f-name').waitFor({timeout: 10000});
    await b.page.getByLabel('Search channels').fill('zzzzz');
    await b.page.waitForTimeout(500);
    await shot(b.page, '13-search-empty');
    await b.page.getByLabel('Search channels').fill('Neon');
    await b.page.waitForTimeout(500);
    await shot(b.page, '14-search-count');
    await b.page.getByLabel('Search channels').fill('');
    await b.page.waitForTimeout(400);
    await b.page.locator('.jw-group-header', {hasText: 'Networks'}).first().click();
    await b.page.waitForTimeout(300);
    await shot(b.page, '15-collapsed');
    await b.context.close();
  }
  {
    const b = await boot(browser);
    await b.page.locator('#f-name').waitFor({timeout: 10000});
    await b.page.getByRole('option', {name: /Off Air Radio/}).click();
    await b.page.waitForTimeout(1100);
    await shot(b.page, '11-offair-pill');
    await b.context.close();
  }
  {
    const b = await boot(browser, {width: 390, height: 844});
    await b.page.locator('#f-name').waitFor({timeout: 10000});
    await b.page.waitForTimeout(700);
    await shot(b.page, '12-narrow');
    await b.context.close();
  }
  await browser.close();
  console.log('done -> ' + outDir);
})().catch((e) => { console.error(e); process.exit(1); });
