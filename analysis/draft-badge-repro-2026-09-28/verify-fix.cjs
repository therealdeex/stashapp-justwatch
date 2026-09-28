// Deterministic verification of the Apply-receipt poll fix (2026-09-28):
//
// 1. Apply normally; install a +120s setTimeout penalty right after the click
//    — simulates Chrome's intensive throttling of CHAINED timers in a
//    hidden/occluded/backgrounded tab (poll iteration 2+ never fires).
// 2. Assert the bar stalls on "Applying…" while the receipt is already
//    durable server-side (the pre-fix behaviour the owner hit).
// 3. Dispatch `visibilitychange` — the user returning to the tab.
// 4. Assert the bar resolves to "Applied at rN" promptly and the draft chip
//    clears.
//
//   node verify-fix.cjs
const fs = require('fs');
const path = require('path');
const {chromium} = require(process.env.JW_AUDIT_PLAYWRIGHT || '/tmp/stash-verify/node_modules/playwright-core');
const KEY = fs.readFileSync('/opt/stash-dev/API_KEY', 'utf8').trim();
const root = path.resolve(__dirname, '../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';
const FAS = { faTags:{iconName:'tags',icon:[512,512,[],'f02c']}, faUser:{iconName:'user',icon:[448,512,[],'f007']},
  faBuilding:{iconName:'building',icon:[384,512,[],'f1ad']}, faTrashAlt:{iconName:'trash-alt',icon:[448,512,[],'f2ed']} };
(async () => {
  const browser = await chromium.launch({executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome', headless: true, args: ['--no-sandbox']});
  const page = await (await browser.newContext({viewport: {width: 1500, height: 950}})).newPage();
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
    if (scripts[url.pathname]) return route.fulfill({contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: scripts[url.pathname]});
    if (url.host === 'live.invalid' && !url.pathname.startsWith('/graphql') && !url.pathname.startsWith('/plugin'))
      return route.fulfill({contentType: 'text/html', body: html});
    try {
      const req = route.request();
      const headers = {'ApiKey': KEY};
      if (req.method() === 'POST') headers['Content-Type'] = 'application/json';
      const resp = await fetch('http://localhost:9998' + url.pathname + url.search, {method: req.method(), headers, body: req.method() === 'POST' ? req.postData() : undefined});
      const buf = Buffer.from(await resp.arrayBuffer());
      return route.fulfill({status: resp.status, contentType: resp.headers.get('content-type') || 'application/octet-stream', body: buf});
    } catch (e) { return route.fulfill({status: 502, body: String(e)}); }
  });
  const settle = (ms) => new Promise((r) => setTimeout(r, ms));
  const barText = async () => (await page.locator('.jw-apply-wrap').textContent().catch(() => '') || '').trim();
  let fail = 0;
  const check = (name, ok, detail) => { console.log((ok ? '  OK ' : '  FAIL ') + name + (ok ? '' : ' — ' + JSON.stringify(detail))); if (!ok) fail = 1; };

  await page.goto('http://live.invalid/');
  await page.locator('.jw-studio').waitFor({timeout: 20000});
  await settle(2500);
  await page.fill('.jw-search-input', 'Feature Length');
  await settle(800);
  await page.locator('.jw-dial-row', {hasText: 'Feature Length'}).first().click();
  await settle(1500);

  // toggle the inactive sort chip
  const chipTxts = await page.locator('.jw-chip').allTextContents();
  const activeTxts = await page.locator('.jw-chip.jw-chip-active').allTextContents();
  const targetChip = chipTxts.find((t) => !activeTxts.includes(t));
  console.log('toggling sort to', JSON.stringify(targetChip));
  await page.locator('.jw-chip', {hasText: targetChip}).first().click();
  await settle(400);
  await page.click('#apply-btn');
  // throttled-tab simulation: every timer scheduled from now on is delayed +120s
  await page.evaluate(() => {
    const st = window.setTimeout.bind(window);
    window.setTimeout = (fn, ms, ...a) => st(fn, (ms || 0) + 120000, ...a);
  });
  await settle(4500);
  const stalled = await barText();
  check('bar stalls while timers are throttled (receipt already durable server-side)',
    stalled.startsWith('Applying'), stalled.slice(0, 60));
  // the user returns to the tab:
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await settle(2500);
  const resolved = await barText();
  check('bar resolves to Applied after visibilitychange wake', /^Applied at r\d+/.test(resolved), resolved.slice(0, 60));
  const chip = await page.locator('.jw-editor-head-badges span').allTextContents();
  check('header draft chip cleared', !chip.includes('draft'), chip);
  if (errors.length) { console.log('PAGE ERRORS:', errors.slice(0, 5)); fail = 1; }
  await browser.close();
  process.exit(fail);
})().catch((e) => { console.error(e); process.exit(1); });
