// One toggle back to Shuffle to leave the dev library as found.
const fs = require('fs');
const path = require('path');
const {chromium} = require('/tmp/stash-verify/node_modules/playwright-core');
const KEY = fs.readFileSync('/opt/stash-dev/API_KEY', 'utf8').trim();
const root = path.resolve(__dirname, '../..');
const deps = '/tmp/jw-curation-audit-browser/node_modules';
const FAS = {};
(async () => {
  const browser = await chromium.launch({executablePath: '/opt/google/chrome/chrome', headless: true, args: ['--no-sandbox']});
  const page = await (await browser.newContext()).newPage();
  const scripts = {
    '/react.js': fs.readFileSync(path.join(deps, 'react/umd/react.development.js')),
    '/react-dom.js': fs.readFileSync(path.join(deps, 'react-dom/umd/react-dom.development.js')),
    '/ui.js': fs.readFileSync(path.join(root, 'ui/index.js')),
    '/ui.css': fs.readFileSync(path.join(root, 'ui/styles.css')),
  };
  const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/ui.css"><body style="margin:0"><div id="root"></div>
<script src="/react.js"></script><script src="/react-dom.js"></script>
<script>window.PluginApi={React,libraries:{FontAwesomeSolid:{}},components:{},register:{route(p,c){window.AuditApp=c}}};</script>
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
      return route.fulfill({status: resp.status, contentType: resp.headers.get('content-type') || 'application/octet-stream', body: Buffer.from(await resp.arrayBuffer())});
    } catch (e) { return route.fulfill({status: 502, body: String(e)}); }
  });
  const settle = (ms) => new Promise((r) => setTimeout(r, ms));
  await page.goto('http://live.invalid/');
  await page.locator('.jw-studio').waitFor({timeout: 20000});
  await settle(2500);
  await page.fill('.jw-search-input', 'Feature Length');
  await settle(800);
  await page.locator('.jw-dial-row', {hasText: 'Feature Length'}).first().click();
  await settle(1500);
  const active = await page.locator('.jw-chip.jw-chip-active').allTextContents();
  if (active.includes('Shuffle')) { console.log('already Shuffle — nothing to do'); }
  else {
    await page.locator('.jw-chip', {hasText: 'Shuffle'}).first().click();
    await settle(400);
    await page.click('#apply-btn');
    await settle(5000);
  }
  console.log('bar:', JSON.stringify(((await page.locator('.jw-apply-wrap').textContent().catch(() => '')) || '').trim().slice(0, 40)));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
