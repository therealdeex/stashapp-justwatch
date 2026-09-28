// Live smoke test of the 2026-09-28 UI polish against dev Stash (:9998).
// Renders the REAL ui/index.js + ui/styles.css standalone (minimal PluginApi
// shim, as in browser-verify.cjs) but PROXIES every /graphql + asset request
// to the real dev Stash with the API key — real library, real Apply commits,
// real receipts. (Dev web login is interactive-only, so the full Stash shell
// can't be driven headless; this exercises everything the plugin UI does.)
//
//   node live-smoke.cjs
const fs = require('fs');
const path = require('path');
const {chromium} = require(process.env.JW_AUDIT_PLAYWRIGHT || '/tmp/stash-verify/node_modules/playwright-core');
const KEY = fs.readFileSync('/opt/stash-dev/API_KEY', 'utf8').trim();
const BASE = 'http://localhost:9998';
const root = path.resolve(__dirname, '../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';

const failures = [];
const check = (name, ok, detail) => {
  console.log((ok ? '  ✓ ' : '  ✗ ') + name + (ok ? '' : ' — ' + JSON.stringify(detail)));
  if (!ok) failures.push(name);
};
const settle = (ms) => new Promise((r) => setTimeout(r, ms));

const FAS = {
  faTags: {iconName: 'tags', icon: [512, 512, [], 'f02c']},
  faUser: {iconName: 'user', icon: [448, 512, [], 'f007']},
  faBuilding: {iconName: 'building', icon: [384, 512, [], 'f1ad']},
  faTrashAlt: {iconName: 'trash-alt', icon: [448, 512, [], 'f2ed']},
};

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome',
    headless: true, args: ['--no-sandbox']});
  const context = await browser.newContext({viewport: {width: 1500, height: 950}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));

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
  components:{Icon: function Icon(p){ return React.createElement('span', {className:p.className, style:p.style,
    'data-icon': (p.icon && p.icon.iconName) || ''}, (p.icon && p.icon.iconName) || ''); }},
  register:{route(p,c){window.AuditApp=c}}};
</script>
<script src="/ui.js"></script>
<script>ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(window.AuditApp));</script>`;

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (scripts[url.pathname]) {
      return route.fulfill({contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript',
        body: scripts[url.pathname]});
    }
    if (url.host === 'live.invalid' && !url.pathname.startsWith('/graphql') && !url.pathname.startsWith('/plugin')) {
      return route.fulfill({contentType: 'text/html', body: html});
    }
    // proxy to the real dev Stash (GraphQL + plugin asset mirrors)
    try {
      const req = route.request();
      const headers = {'ApiKey': KEY};
      if (req.method() === 'POST') headers['Content-Type'] = 'application/json';
      const resp = await fetch(BASE + url.pathname + url.search, {
        method: req.method(), headers, body: req.method() === 'POST' ? req.postData() : undefined});
      const buf = Buffer.from(await resp.arrayBuffer());
      return route.fulfill({status: resp.status,
        contentType: resp.headers.get('content-type') || 'application/octet-stream', body: buf});
    } catch (e) {
      return route.fulfill({status: 502, body: String(e)});
    }
  });

  await page.goto('http://live.invalid/');
  await page.locator('.jw-studio').waitFor({timeout: 20000});
  await settle(2000);
  check('Channel Studio boots against real dev library', true);

  const dialCount = await page.locator('.jw-dial-row').count();
  console.log('  dial rows: ' + dialCount);
  check('channels loaded from dev Stash', dialCount >= 1, dialCount);

  // pick the first custom channel that has a populated facet (chips + Clear all)
  let chanName = null, row = null, clearAlls = 0;
  const rows = page.locator('.jw-dial-row');
  for (let i = 0; i < Math.min(dialCount, 12); i++) {
    await rows.nth(i).click();
    await settle(900);
    const n = await page.locator('.jw-chipbar-clear').count();
    if (n >= 1) { row = i; clearAlls = n; chanName = await page.locator('#f-name').inputValue(); break; }
  }
  if (!chanName) {
    await rows.first().click();
    await settle(900);
    row = 0;
    chanName = await page.locator('#f-name').inputValue();
  }
  console.log('  channel under test: "' + chanName + '" (dial row ' + row + ', Clear-all toolbars: ' + clearAlls + ')');
  check('a channel with Clear-all chip toolbars exists', clearAlls >= 1);

  // play-order chips
  const progCard = page.locator('.jw-card', {hasText: 'Play order'});
  check('six play-order chips', await progCard.locator('.jw-chip-row .jw-chip').count() === 6,
    await progCard.locator('.jw-chip-row .jw-chip').count());
  const activeBefore = (await progCard.locator('.jw-chip-active').textContent()).trim();
  const targetLabel = (await progCard.locator('.jw-chip-row .jw-chip:not(.jw-chip-active)')
    .first().textContent()).trim();
  const target = progCard.getByRole('button', {name: targetLabel, exact: true}).first();
  await target.click();
  await settle(400);
  check('play-order chip activates (' + activeBefore + ' -> ' + targetLabel + ')',
    await target.evaluate((el) => el.classList.contains('jw-chip-active')));
  check('dirty pill appears', await page.locator('.jw-pill-dirty').count() >= 1);

  // draft survives reload (sessionStorage) — same selection, still dirty
  await page.reload({waitUntil: 'load'});
  await page.locator('.jw-studio').waitFor({timeout: 20000});
  await settle(2500);
  check('draft survives page reload', await page.locator('.jw-pill-dirty').count() >= 1);
  // the editor does not restore the channel selection — re-open the drafted channel
  await page.locator('.jw-dial-row').nth(row).click();
  await settle(1500);
  check('chip selection survives reload', await page.locator('.jw-card', {hasText: 'Play order'})
    .getByRole('button', {name: targetLabel, exact: true}).first()
    .evaluate((el) => el.classList.contains('jw-chip-active')));

  // the restored draft revalidates against the real server — wait until Apply arms
  await page.locator('#apply-btn:not([disabled])').waitFor({timeout: 40000});

  // Apply via Ctrl+Enter → receipt path
  await page.locator('#f-name').press('Control+Enter');
  const applied = await page.locator('text=Applied at r').first()
    .waitFor({timeout: 40000}).then(() => true).catch(() => false);
  if (!applied) {
    console.log('  apply-state area: ' + await page.locator('.jw-action-bar, .jw-apply-state')
      .allTextContents().catch(() => 'n/a'));
    console.log('  toasts: ' + await page.locator('.jw-toast').allTextContents().catch(() => 'n/a'));
  }
  check('Apply commits against dev (receipt shown)', applied);
  check('dirty pill cleared after Apply', await page.locator('.jw-pill-dirty').count() === 0);

  // restore the original play order so dev is left as found
  if (applied) {
    const pc3 = page.locator('.jw-card', {hasText: 'Play order'});
    await pc3.getByRole('button', {name: activeBefore, exact: true}).first().click();
    await settle(400);
    await page.locator('#apply-btn:not([disabled])').waitFor({timeout: 40000});
    await page.locator('#f-name').press('Control+Enter');
    const restored = await page.locator('text=Applied at r').first()
      .waitFor({timeout: 40000}).then(() => true).catch(() => false);
    console.log('  play order restored to "' + activeBefore + '": ' + restored);
  }

  check('no page errors', errors.length === 0, errors.slice(0, 5));
  await browser.close();
  if (failures.length) { console.error('\nFAILURES: ' + failures.join(' | ')); process.exit(1); }
  console.log('\nlive smoke passed (dev Stash :9998)');
})().catch((e) => { console.error(e); process.exit(1); });
