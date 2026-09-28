// Reproduce: edit the play order on net_4a1078b4 (Feature Length), Apply,
// and watch the header "draft" chip / bar / drafts store through the receipt.
const fs = require('fs');
const path = require('path');
const {chromium} = require(process.env.JW_AUDIT_PLAYWRIGHT || '/tmp/stash-verify/node_modules/playwright-core');
const KEY = fs.readFileSync('/opt/stash-dev/API_KEY', 'utf8').trim();
const BASE = 'http://localhost:9998';
const root = path.resolve(__dirname, '../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';

const FAS = {
  faTags: {iconName: 'tags', icon: [512, 512, [], 'f02c']},
  faUser: {iconName: 'user', icon: [448, 512, [], 'f007']},
  faBuilding: {iconName: 'building', icon: [384, 512, [], 'f1ad']},
  faTrashAlt: {iconName: 'trash-alt', icon: [448, 512, [], 'f2ed']},
};
(async () => {
  const browser = await chromium.launch({executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome', headless: true, args: ['--no-sandbox']});
  const context = await browser.newContext({viewport: {width: 1500, height: 950}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

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
    if (scripts[url.pathname]) return route.fulfill({contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: scripts[url.pathname]});
    if (url.host === 'live.invalid' && !url.pathname.startsWith('/graphql') && !url.pathname.startsWith('/plugin'))
      return route.fulfill({contentType: 'text/html', body: html});
    try {
      const req = route.request();
      const headers = {'ApiKey': KEY};
      if (req.method() === 'POST') headers['Content-Type'] = 'application/json';
      const resp = await fetch(BASE + url.pathname + url.search, {method: req.method(), headers, body: req.method() === 'POST' ? req.postData() : undefined});
      const buf = Buffer.from(await resp.arrayBuffer());
      return route.fulfill({status: resp.status, contentType: resp.headers.get('content-type') || 'application/octet-stream', body: buf});
    } catch (e) { return route.fulfill({status: 502, body: String(e)}); }
  });

  const settle = (ms) => new Promise((r) => setTimeout(r, ms));
  await page.goto('http://live.invalid/');
  await page.locator('.jw-studio').waitFor({timeout: 20000});
  await settle(2500);

  // search for the Feature Length channel in the dial
  await page.fill('.jw-dial-search input, input[type="search"], #dial-search', 'Feature Length').catch(async () => {
    // fall back: find any text input in the rail
    const inputs = page.locator('.jw-rail input');
    await inputs.first().fill('Feature Length');
  });
  await settle(800);
  const rows = page.locator('.jw-dial-row', {hasText: 'Feature Length'});
  const n = await rows.count();
  console.log('rows matching Feature Length:', n);
  if (!n) { console.log('FAIL: channel not found'); process.exit(1); }
  await rows.first().click();
  await settle(1500);

  const snap = async (label) => {
    const chip = await page.locator('.jw-editor-head-badges .jw-chip, .jw-editor-head-badges span').allTextContents().catch(() => []);
    const bar = await page.locator('.jw-apply-wrap').textContent().catch(() => '');
    const btn = page.locator('#apply-btn');
    const btnDisabled = await btn.getAttribute('disabled').catch(() => null);
    const btnText = await btn.textContent().catch(() => '');
    const store = await page.evaluate(() => { const k = Object.keys(sessionStorage).find(k => k.startsWith('jw-studio-drafts')); return k ? JSON.parse(sessionStorage.getItem(k)) : {}; });
    const entry = store && store['net_4a1078b4'];
    console.log('[' + label + '] chips=' + JSON.stringify(chip) + ' | bar=' + JSON.stringify((bar||'').trim().slice(0,90))
      + ' | btn=' + btnText.trim() + (btnDisabled != null ? '(disabled)' : '(enabled)')
      + ' | draftStore=' + (entry ? 'draft.sort=' + (entry.draft && entry.draft.sort) + ' pending=' + JSON.stringify(entry.pending ? entry.pending.requestId : null) : 'NONE'));
  };

  await snap('loaded');

  // click the "Newest" play-order chip (or Shuffle if already newest)
  const activeChip = await page.locator('.jw-chip.jw-chip-active').allTextContents();
  console.log('active sort chip:', JSON.stringify(activeChip));
  const target = activeChip.includes('Newest') ? 'Shuffle' : 'Newest';
  console.log('clicking chip:', target);
  await page.locator('.jw-chip', {hasText: target}).first().click();
  await settle(600);
  await snap('edited');

  await page.click('#apply-btn');
  for (const t of [300, 800, 1500, 2500, 4000, 6000, 9000]) {
    await settle(t === 300 ? 300 : t - (await Promise.resolve()));
    await snap('t+' + t + 'ms');
  }

  // leave the library how we found it: if we changed to Newest, revert to Shuffle
  const bar = await page.locator('.jw-apply-wrap').textContent().catch(() => '');
  console.log('final bar:', JSON.stringify((bar||'').trim()));
  if (errors.length) console.log('PAGE ERRORS:', errors.slice(0, 5));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
