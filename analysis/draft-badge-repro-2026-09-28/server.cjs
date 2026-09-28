// Tiny HTTP server: serves the shim page + ui files, proxies /graphql to dev Stash.
const http = require('http');
const fs = require('fs');
const path = require('path');
const KEY = fs.readFileSync('/opt/stash-dev/API_KEY', 'utf8').trim();
const BASE = 'http://localhost:9998';
const root = path.resolve(__dirname, '../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';
const FAS = { faTags:{iconName:'tags',icon:[512,512,[],'f02c']}, faUser:{iconName:'user',icon:[448,512,[],'f007']},
  faBuilding:{iconName:'building',icon:[384,512,[],'f1ad']}, faTrashAlt:{iconName:'trash-alt',icon:[448,512,[],'f2ed']} };
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
      let outcome = null, err = null;
      try { const j = await res.clone().json(); outcome = j && j.data ? JSON.stringify(j.data).slice(0,120) : null; err = j && j.errors ? j.errors[0].message : null; } catch (e) { err = 'non-json'; }
      window.__gqlLog.push({t: Date.now(), mode, status: res.status, outcome, err});
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
const files = {
  '/': [html, 'text/html'],
  '/react.js': [fs.readFileSync(path.join(deps, 'react/umd/react.development.js')), 'text/javascript'],
  '/react-dom.js': [fs.readFileSync(path.join(deps, 'react-dom/umd/react-dom.development.js')), 'text/javascript'],
  '/ui.js': [fs.readFileSync(path.join(root, 'ui/index.js')), 'text/javascript'],
  '/ui.css': [fs.readFileSync(path.join(root, 'ui/styles.css')), 'text/css'],
};
http.createServer(async (req, res) => {
  if (files[req.url]) { res.writeHead(200, {'content-type': files[req.url][1]}); return res.end(files[req.url][0]); }
  const t0 = Date.now();
  try {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = Buffer.concat(chunks);
    let mode = '';
    if (body.length) { try { const b = JSON.parse(body.toString()); mode = (b.variables && b.variables.args && b.variables.args.mode) || (b.variables && b.variables.name) || ''; } catch (e) {} }
    const headers = {'ApiKey': KEY};
    if (req.method === 'POST') headers['Content-Type'] = 'application/json';
    const upstream = await fetch(BASE + req.url, {method: req.method, headers, body: body.length ? body : undefined});
    const buf = Buffer.from(await upstream.arrayBuffer());
    if (req.url === '/graphql') console.log('  [srv] ' + mode + ' -> ' + upstream.status + ' in ' + (Date.now() - t0) + 'ms');
    res.writeHead(upstream.status, {'content-type': upstream.headers.get('content-type') || 'application/octet-stream'});
    res.end(buf);
  } catch (e) { res.writeHead(502); res.end(String(e)); }
}).listen(8871, () => console.log('listening on 8871'));
