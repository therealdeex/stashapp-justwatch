// Async-Apply UI probes for the CURRENT ui/index.js (2026-09-26 change:
// background applies with free navigation). Adapted from
// ../channel-edit-audit-2026-09-24/browser-fix-verify.cjs. GraphQL is mocked;
// sources are validated with the REAL justwatch.criteria module via
// bridge_validate.py. The Apply task response is gated so a receipt can be
// released on demand while the UI keeps running.
//
// Scenarios:
//   free_navigation   apply → switch channels with no dialog → receipt lands
//                     in the background → named toast, pill clears
//   reload_mid_apply  Reload is no longer blocked while an apply is in flight
//   remount_dedup     leaving and returning before the receipt lands must
//                     produce exactly ONE settlement toast
const fs = require('fs');
const path = require('path');
const {execFileSync} = require('child_process');
const {chromium} = require(process.env.JW_AUDIT_PLAYWRIGHT || '/tmp/stash-verify/node_modules/playwright-core');
const root = path.resolve(__dirname, '../..');
const deps = process.env.JW_AUDIT_REACT_ROOT || '/tmp/jw-curation-audit-browser/node_modules';
const scripts = {
  '/react.js': fs.readFileSync(path.join(deps, 'react/umd/react.development.js')),
  '/react-dom.js': fs.readFileSync(path.join(deps, 'react-dom/umd/react-dom.development.js')),
  '/ui.js': fs.readFileSync(path.join(root, 'ui/index.js')),
  '/ui.css': fs.readFileSync(path.join(root, 'ui/styles.css')),
};
const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/ui.css"><div id="root"></div>
<script src="/react.js"></script><script src="/react-dom.js"></script>
<script>window.PluginApi={React:React,register:{route(p,c){window.AuditApp=c}}};</script>
<script src="/ui.js"></script><script>ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(window.AuditApp));</script>`;
const clone = x => JSON.parse(JSON.stringify(x));

function pyValidate(source) {
  const out = execFileSync('python3', [path.join(__dirname, '../channel-edit-audit-2026-09-24/bridge_validate.py'), root],
    {input: JSON.stringify(source)});
  return JSON.parse(out.toString());
}

function fixture() {
  return {libraryId:'lib_async_apply',revision:1,
    groups:[{id:'grp_my',name:'My Channels',position:1}],
    channels:[1,2].map(i=>({id:`ch_${String(i).padStart(8,'0')}`,kind:'ch',number:i,name:`Channel ${i}`,
      glyph:null,color:'#112233',groupId:'grp_my',sort:'shuffle',seed:i,enabled:true,archived:false,paused:false,
      source:{type:'filter',tags:['5'],duration:{min:3600}},sourceLabel:'Tag',programming:{mode:'fixed'},provenance:{origin:'custom'}}))};
}

async function boot(browser) {
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage();
  let releaseGate = null;
  const state = {doc:fixture(),tasks:[],taskSeen:0,receipts:[],errors:[],
    operations:[],gated:false,gate:null,
    release() { if (releaseGate) releaseGate(); }};
  page.on('pageerror', e => state.errors.push(e.message));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/graphql') {
      const body = route.request().postDataJSON(), vars = body.variables || {};
      let data = {};
      if (body.query.includes('runPluginTask')) {
        state.taskSeen++;
        if (state.gated) await state.gate; // hold the task response until released
        const args = vars.a, ops = JSON.parse(args.ops);
        const bad = [];
        for (const op of ops) {
          const src = op.channel && op.channel.source;
          if (src) for (const e of pyValidate(src).errors) bad.push(Object.assign({path: `ops[0].channel.${e.path}`}, e));
        }
        if (bad.length) {
          state.receipts[args.requestId] = {requestId:args.requestId,status:'rejected',error:'validation_failed',errors:bad,message:'validation failed'};
        } else if (Number(args.expectedRevision) !== state.doc.revision) {
          state.receipts[args.requestId] = {requestId:args.requestId,status:'rejected',error:'revision_conflict',currentRevision:state.doc.revision};
        } else {
          const newIds = {};
          for (const op of ops) {
            if (op.op === 'channel.put') state.doc.channels = state.doc.channels.map(c => c.id === op.channel.id ? clone(op.channel) : c);
            if (op.op === 'channel.create') {
              const id = 'ch_' + String(state.doc.channels.length + 1).padStart(8,'0');
              newIds[op.tempId] = id;
              state.doc.channels.push(Object.assign({}, clone(op.channel), {id, seed:77}));
            }
          }
          state.doc.revision++;
          state.receipts[args.requestId] = {requestId:args.requestId,status:'committed',revision:state.doc.revision,
            refresh:{}, ...(Object.keys(newIds).length ? {idMap:newIds} : {})};
        }
        state.tasks.push(clone(args));
        data = {runPluginTask:'job-async'};
      } else if (body.query.includes('runPluginOperation')) {
        const a = vars.args;
        state.operations.push(clone(a));
        let out;
        switch (a.mode) {
          case 'GetChannelLibrary': out = {...clone(state.doc), total: state.doc.channels.length}; break;
          case 'GetChannelDefinition': {
            const ch = state.doc.channels.find(c => c.id === a.channelId);
            out = {revision: state.doc.revision, channel: ch ? clone(ch) : null, summary: []}; break;
          }
          case 'ValidateChannelChanges': {
            const ops = JSON.parse(a.ops);
            const errors = [];
            for (const op of ops) {
              const src = op.channel && op.channel.source;
              if (src) for (const e of pyValidate(src).errors) errors.push(Object.assign({path:`ops[0].channel.${e.path}`}, e));
            }
            out = {valid: !errors.length, errors, effects: [], revision: state.doc.revision}; break;
          }
          case 'GetChannelApplyResult': out = state.receipts[a.requestId] || {status:'unknown'}; break;
          case 'GetChannelRefreshStatus': out = {libraryRevision: state.doc.revision, snapshotRevision: state.doc.revision,
            computedAt: null, channelId: a.channelId, pending: null,
            health: {healthStatus: 'ok', sceneCount: 1}}; break;
          default: out = {};
        }
        data = {runPluginOperation: out};
      } else data = {findTag:{name:'Tag 5'},findPerformer:{name:'Performer'},findStudio:{name:'Studio'}};
      return route.fulfill({contentType:'application/json',body:JSON.stringify({data})});
    }
    if (scripts[url.pathname]) return route.fulfill({contentType:url.pathname.endsWith('.css')?'text/css':'text/javascript',body:scripts[url.pathname]});
    return route.fulfill({contentType:'text/html',body:html});
  });
  await page.goto('http://audit.invalid/');
  await page.locator('#f-name').waitFor({timeout:10000});
  state.hold = () => { state.gated = true; state.gate = new Promise((r) => { releaseGate = r; }); };
  return {context, page, state};
}
const waitSeen = async (page, state, count) => {
  for (let i = 0; i < 60 && state.taskSeen < count; i++) await page.waitForTimeout(100);
  if (state.taskSeen < count) throw Error('apply task not submitted');
};
const failures = [];
function check(name, ok, detail) {
  if (!ok) failures.push(name + (detail ? ': ' + JSON.stringify(detail) : ''));
  return ok;
}

(async () => {
 const browser = await chromium.launch({executablePath:process.env.JW_AUDIT_CHROME||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const results = {};
 try {
  // -- free navigation: leaving mid-apply is silent and the receipt lands ----
  // -- in the background with a named toast ----------------------------------
  {
   const b = await boot(browser);
   b.state.hold();
   await b.page.locator('#f-name').fill('Renamed One');
   await b.page.locator('#apply-btn').click();
   await waitSeen(b.page, b.state, 1);
   await b.page.waitForTimeout(300);
   const barApplying = await b.page.getByText(/Applying… — you can keep editing or switch channels/).count();
   const pillApplying = await b.page.getByText('Applying “Renamed One”…').count();
   // leave: no confirm dialog, channel 2 mounts while the apply runs
   await b.page.getByRole('option', {name: /2 · Channel 2/}).click();
   await b.page.waitForTimeout(300);
   const dialogs = await b.page.getByRole('dialog').count();
   const ch2Name = await b.page.locator('#f-name').inputValue();
   const pillWhileAway = await b.page.getByText('Applying “Renamed One”…').count();
   b.state.release();
   await b.page.waitForTimeout(1500); // first receipt poll lands ~700ms after the task response
   const landedToast = await b.page.getByText(/Applied “Renamed One” at r2\./).count();
   await b.page.waitForTimeout(300);
   const pillAfter = await b.page.getByText('Applying “Renamed One”…').count();
   results.free_navigation = {barApplying, pillApplying, dialogs, ch2Name, pillWhileAway, landedToast, pillAfter,
     committedName: b.state.doc.channels[0].name, errors: b.state.errors};
   check('free_navigation: bar invites continued work', barApplying >= 1, {barApplying});
   check('free_navigation: top-bar pill names the applying channel', pillApplying >= 1, {pillApplying});
   check('free_navigation: no confirm dialog on switch', dialogs === 0, {dialogs});
   check('free_navigation: channel 2 editor mounted', ch2Name === 'Channel 2', {ch2Name});
   check('free_navigation: pill persists while away', pillWhileAway >= 1, {pillWhileAway});
   check('free_navigation: named toast lands in the background', landedToast >= 1, {landedToast});
   check('free_navigation: pill clears after the receipt', pillAfter === 0, {pillAfter});
   check('free_navigation: server committed the rename', b.state.doc.channels[0].name === 'Renamed One', b.state.doc.channels[0]);
   check('free_navigation: no page errors', b.state.errors.length === 0, b.state.errors);
   await b.context.close();
  }
  // -- reload is allowed mid-apply --------------------------------------------
  {
   const b = await boot(browser);
   b.state.hold();
   await b.page.locator('#f-name').fill('Renamed One');
   await b.page.locator('#apply-btn').click();
   await waitSeen(b.page, b.state, 1);
   const libsBefore = b.state.operations.filter(o => o.mode === 'GetChannelLibrary').length;
   await b.page.getByRole('button', {name: 'Reload', exact: true}).click();
   await b.page.waitForTimeout(600);
   const libsAfter = b.state.operations.filter(o => o.mode === 'GetChannelLibrary').length;
   const blockedToast = await b.page.getByText(/Reload is disabled/).count();
   b.state.release();
   await b.page.waitForTimeout(1500);
   // user stayed on the channel: success feedback is the bar state, not a toast
   const landedBar = await b.page.getByText(/Applied at r2\./).count();
   const committed = b.state.doc.revision === 2 && b.state.doc.channels[0].name === 'Renamed One';
   results.reload_mid_apply = {libsBefore, libsAfter, blockedToast, landedBar, committed, errors: b.state.errors};
   check('reload_mid_apply: library refetch ran', libsAfter > libsBefore, {libsBefore, libsAfter});
   check('reload_mid_apply: no disabled-reload error', blockedToast === 0, {blockedToast});
   check('reload_mid_apply: receipt still landed after reload', landedBar >= 1 && committed, {landedBar, committed});
   check('reload_mid_apply: no page errors', b.state.errors.length === 0, b.state.errors);
   await b.context.close();
  }
  // -- remount dedup: leave + return before the receipt → ONE toast ----------
  {
   const b = await boot(browser);
   b.state.hold();
   await b.page.locator('#f-name').fill('Renamed One');
   await b.page.locator('#apply-btn').click();
   await waitSeen(b.page, b.state, 1);
   await b.page.getByRole('option', {name: /2 · Channel 2/}).click();
   await b.page.waitForTimeout(300);
   await b.page.getByRole('option', {name: /1 · Channel 1/}).click(); // remount re-attaches to the same promise
   await b.page.waitForTimeout(400);
   b.state.release();
   await b.page.waitForTimeout(1500);
   const appliedToasts = await b.page.getByText(/Applied “Renamed One” at r2\./).count();
   results.remount_dedup = {appliedToasts, errors: b.state.errors};
   check('remount_dedup: exactly one settlement toast', appliedToasts === 1, {appliedToasts});
   check('remount_dedup: no page errors', b.state.errors.length === 0, b.state.errors);
   await b.context.close();
  }
 } finally {await browser.close();}
 results.failures = failures;
 fs.writeFileSync(path.join(__dirname,'results.json'),JSON.stringify(results,null,2)+'\n');
 console.log(JSON.stringify(results,null,2));
 if (failures.length) { console.error('FAILURES:\n' + failures.map(f => '  ✗ ' + f).join('\n')); process.exitCode = 1; }
})().catch(e=>{console.error(e);process.exitCode=1;});
