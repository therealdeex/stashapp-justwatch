// Instrumented repro: log every /graphql exchange around Apply + the receipt poll.
const fs = require('fs');
const path = require('path');
const {chromium} = require(process.env.JW_AUDIT_PLAYWRIGHT || '/tmp/stash-verify/node_modules/playwright-core');
const KEY = fs.readFileSync('/opt/stash-dev/API_KEY', 'utf8').trim();
const BASE = 'http://localhost:9998';
const root = path.resolve(__dirname, '../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';
const FAS = { faTags: {iconName:'tags',icon:[512,512,[],'f02c']}, faUser:{iconName:'user',icon:[448,512,[],'f007']},
  faBuilding:{iconName:'building',icon:[384,512,[],'f1ad']}, faTrashAlt:{iconName:'trash-alt',icon:[448,512,[],'f2ed']} };
(async () => {
  const browser = await chromium.launch({executablePath: process.env.JW_AUDIT_CHROME || '/opt/google/chrome/chrome', headless: true, args: ['--no-sandbox']});
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
<script>window.__gqlLog = [];
const _fetch = window.fetch.bind(window);
window.fetch = async function(url, opts) {
  const res = await _fetch(url, opts);
  try {
    if (String(url).includes('/graphql')) {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      const mode = body && body.variables && body.variables.args ? body.variables.args.mode : (body && body.variables ? body.variables.name : '?');
      const clone = await res.clone().json().catch(() => null);
      window.__gqlLog.push({mode, status: res.status, body: clone && clone.data && clone.data.runPluginOperation !== undefined ? clone.data : clone, err: clone && clone.errors ? clone.errors[0].message : null});
    }
  } catch (e) {}
  return res;
};</script>
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
  await page.fill('.jw-search-input', 'Feature Length'); // REAL rail search — filter ACTIVE
  await settle(800);
  const visibleRows = await page.locator('.jw-dial-row').count();
  console.log('dial rows after search:', visibleRows);
  await page.locator('.jw-dial-row', {hasText: 'Feature Length'}).first().click();
  await settle(1500);

  for (let iter = 0; iter < 3; iter++) {
    const before = await page.evaluate(() => window.__gqlLog.length);
    const chipTxts = await page.locator('.jw-chip').allTextContents();
    const activeTxts = await page.locator('.jw-chip.jw-chip-active').allTextContents();
    const targetChip = chipTxts.find((t) => !activeTxts.includes(t));
    console.log('--- iter ' + iter + ': toggling sort to ' + JSON.stringify(targetChip));
    await page.locator('.jw-chip', {hasText: targetChip}).first().click();
    await settle(400);
    await page.click('#apply-btn');
    await settle(8000);
    const log = await page.evaluate(() => window.__gqlLog);
    const polls = log.slice(before).filter((e) => e.mode === 'GetChannelApplyResult');
    const others = log.slice(before).filter((e) => e.mode !== 'GetChannelApplyResult').map((e) => e.mode + '(' + e.status + (e.err ? ':' + e.err.slice(0,80) : '') + ')');
    const bar = await page.locator('.jw-apply-wrap').textContent().catch(() => '');
    console.log('    pre-poll ops:', others.join(' '));
    console.log('    polls:', polls.length, polls.map((p) => 'http=' + p.status + (p.err ? ' ERR=' + p.err.slice(0, 100) : ' ok=' + JSON.stringify(p.body && p.body.runPluginOperation && p.body.runPluginOperation.status)).slice(0, 80)).join(' | '));
    console.log('    bar:', JSON.stringify((bar||'').trim().slice(0, 60)));
  }
  if (errors.length) console.log('PAGE ERRORS:', errors.slice(0, 5));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
