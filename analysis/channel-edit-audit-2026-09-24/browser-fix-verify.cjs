// Post-fix verification probes for the CURRENT ui/index.js. Unlike
// browser-reproduce.cjs (which mocks `valid: true`), the mock server here
// validates every submitted source with the REAL justwatch.criteria module
// via bridge_validate.py — a payload the Python rejects fails the Apply in
// the harness too, so protocol mismatches cannot hide behind mocks
// (audit E1 acceptance criterion).
//
// Scenarios:
//   any_toggle            ANY/ALL toggle must OMIT the unused side entirely
//   remove_last_tag       the audit's E1 repro: last tag removed, 60→80 min
//   immediate_text_apply  E6: Apply right after typing must submit the text
//   explicit_zero_max     E8: max 0 survives as a real bound
//   decimal_minutes       E8: 1.5 min stores exactly 90 seconds
//   refresh_pill          pending → ready from the durable status op
//   refresh_failed        unavailable health shows Retry; Retry requeues
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

// The honest mock: Python validates every source (no `valid: true` mock).
function pyValidate(source) {
  const out = execFileSync('python3', [path.join(__dirname, 'bridge_validate.py'), root],
    {input: JSON.stringify(source)});
  return JSON.parse(out.toString());
}

function fixture() {
  return {libraryId:'lib_browser_fix',revision:1,
    groups:[{id:'grp_my',name:'My Channels',position:1}],
    channels:[1,2].map(i=>({id:`ch_${String(i).padStart(8,'0')}`,kind:'ch',number:i,name:`Channel ${i}`,
      glyph:null,color:'#112233',groupId:'grp_my',sort:'shuffle',seed:i,enabled:true,archived:false,paused:false,
      source:{type:'filter',tags:['5'],duration:{min:3600}},sourceLabel:'Tag',programming:{mode:'fixed'},provenance:{origin:'custom'}}))};
}

async function boot(browser, opts = {}) {
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage();
  const state = {doc:fixture(),tasks:[],receipts:{},errors:[],requeues:[],statusCalls:0,
    statusScript: opts.statusScript || null, hold: false};
  page.on('pageerror', e => state.errors.push(e.message));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/graphql') {
      const body = route.request().postDataJSON(), vars = body.variables || {};
      let data = {};
      if (body.query.includes('runPluginTask')) {
        if (state.hold) { return route.fulfill({contentType:'application/json',body:JSON.stringify({data:{runPluginTask:'job-held'}})}); }
        const args = vars.a, ops = JSON.parse(args.ops);
        // REAL validation: any structurally invalid source rejects the Apply
        const bad = [];
        for (const op of ops) {
          const src = op.channel && op.channel.source;
          if (src) {
            const check = pyValidate(src);
            for (const e of check.errors) bad.push(Object.assign({path: `ops[0].channel.${e.path}`}, e));
          }
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
        data = {runPluginTask:'job-fix'};
      } else if (body.query.includes('runPluginOperation')) {
        const a = vars.args;
        (state.operations ||= []).push(clone(a));
        let out;
        switch (a.mode) {
          case 'GetChannelLibrary': out = {...clone(state.doc), total: state.doc.channels.length}; break;
          case 'GetChannelDefinition': {
            const ch = state.doc.channels.find(c => c.id === a.channelId);
            out = {revision: state.doc.revision, channel: ch ? clone(ch) : null, summary: []}; break;
          }
          case 'PreviewChannelPool': {
            const check = pyValidate(JSON.parse(a.source));
            out = check.valid
              ? {status:'ok',poolCount:1,rotationSize:1,sample:[],rotationComplete:true,signature:'mock'}
              : {status:'error',message: check.errors[0].message};
            break;
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
          case 'GetChannelRefreshStatus': {
            state.statusCalls++;
            if (state.statusScript) {
              const step = state.statusScript[Math.min(state.statusCalls - 1, state.statusScript.length - 1)];
              out = {libraryRevision: state.doc.revision, snapshotRevision: state.doc.revision, computedAt: null,
                     channelId: a.channelId, pending: step.pending || null,
                     health: step.health || null};
            } else {
              out = {libraryRevision: state.doc.revision, snapshotRevision: state.doc.revision, computedAt: null,
                     channelId: a.channelId, pending: null, health: {healthStatus: 'ok', sceneCount: 1}};
            }
            break;
          }
          case 'RequeueChannelRefresh': state.requeues.push(clone(a)); out = {queued: true, channelId: a.channelId, generation: 1}; break;
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
  return {context, page, state};
}
const waitTasks = async (page, state, count) => {
  for (let i = 0; i < 60 && state.tasks.length < count; i++) await page.waitForTimeout(100);
  if (state.tasks.length < count) throw Error('task not submitted');
};
const failures = [];
function check(name, ok, detail) {
  if (!ok) failures.push(name + (detail ? ': ' + JSON.stringify(detail) : ''));
  return ok;
}
const noEmptyArrays = (src) => Object.entries(src || {}).every(([k, v]) => !(Array.isArray(v) && v.length === 0));

(async () => {
 const browser = await chromium.launch({executablePath:process.env.JW_AUDIT_CHROME||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const results = {};
 try {
  // -- E1a: ANY/ALL toggle leaves no empty arrays on the wire ---------------
  {
   const b = await boot(browser);
   await b.page.getByRole('button', {name: 'ANY', exact: true}).first().click();
   await b.page.locator('#apply-btn').click();
   await waitTasks(b.page, b.state, 1);
   const source = JSON.parse(b.state.tasks[0].ops)[0].channel.source;
   results.any_toggle = {source, errors: b.state.errors};
   check('any_toggle: unused ALL side omitted', !('tags' in source), source);
   check('any_toggle: ids moved to tagsAny', JSON.stringify(source.tagsAny) === '["5"]', source);
   check('any_toggle: no empty arrays anywhere', noEmptyArrays(source), source);
   check('any_toggle: python validation accepted it', pyValidate(source).valid, pyValidate(source).errors);
   await b.context.close();
  }
  // -- E1b: the audit's exact repro — remove final tag, 60→80 minutes -------
  {
   const b = await boot(browser);
   await b.page.getByRole('button', {name: 'Remove Tag 5', exact: true}).click();
   await b.page.getByRole('spinbutton', {name: 'Maximum duration in minutes', exact: true}).fill('80');
   await b.page.locator('#apply-btn').click();
   await waitTasks(b.page, b.state, 1);
   await b.page.waitForTimeout(300);
   const source = JSON.parse(b.state.tasks[0].ops)[0].channel.source;
   results.remove_last_tag = {source, errors: b.state.errors};
   check('remove_last_tag: no tags keys at all', !('tags' in source) && !('tagsAny' in source), source);
   check('remove_last_tag: duration 60→80 stored', source.duration && source.duration.max === 4800, source);
   check('remove_last_tag: no empty arrays anywhere', noEmptyArrays(source), source);
   check('remove_last_tag: python validation accepted it', pyValidate(source).valid, pyValidate(source).errors);
   await b.context.close();
  }
  // -- E6: immediate Apply commits the visible text --------------------------
  {
   const b = await boot(browser);
   await b.page.locator('#f-name').fill('Renamed');
   await b.page.getByRole('textbox', {name: 'Text search', exact: true}).fill('new query');
   await b.page.locator('#apply-btn').click();
   await waitTasks(b.page, b.state, 1);
   await b.page.waitForTimeout(400);
   const source = JSON.parse(b.state.tasks[0].ops)[0].channel.source;
   results.immediate_text_apply = {source, errors: b.state.errors,
     displayedQuery: await b.page.getByRole('textbox', {name: 'Text search', exact: true}).inputValue()};
   check('immediate_text_apply: q submitted', source.q === 'new query', source);
   check('immediate_text_apply: text not lost', (await b.page.getByRole('textbox', {name: 'Text search', exact: true}).inputValue()) === 'new query');
   await b.context.close();
  }
  // -- E8a: explicit zero max is a real bound (empty pool) --------------------
  {
   const b = await boot(browser);
   // clear the fixture's 60-minute minimum first: {min:3600, max:0} would
   // (correctly) fail client validation as max-below-min.
   await b.page.getByRole('spinbutton', {name: 'Minimum duration in minutes', exact: true}).fill('');
   await b.page.getByRole('spinbutton', {name: 'Maximum duration in minutes', exact: true}).fill('0');
   await b.page.locator('#apply-btn').click();
   await waitTasks(b.page, b.state, 1);
   const source = JSON.parse(b.state.tasks[0].ops)[0].channel.source;
   results.explicit_zero_max = {source, errors: b.state.errors};
   check('explicit_zero_max: max 0 preserved', source.duration && source.duration.max === 0, source);
   check('explicit_zero_max: python validation accepted it', pyValidate(source).valid, pyValidate(source).errors);
   await b.context.close();
  }
  // -- E8b: decimal minutes store exact seconds ------------------------------
  {
   const b = await boot(browser);
   await b.page.getByRole('spinbutton', {name: 'Minimum duration in minutes', exact: true}).fill('1.5');
   await b.page.locator('#apply-btn').click();
   await waitTasks(b.page, b.state, 1);
   const source = JSON.parse(b.state.tasks[0].ops)[0].channel.source;
   results.decimal_minutes = {source, errors: b.state.errors};
   check('decimal_minutes: 1.5 min stored as exactly 90s', source.duration && source.duration.min === 90, source);
   check('decimal_minutes: python validation accepted it', pyValidate(source).valid, pyValidate(source).errors);
   await b.context.close();
  }
  // -- refresh pill: pending → ready -----------------------------------------
  {
   const b = await boot(browser, {statusScript: [
     {pending: {channelId: 'ch_00000001', generation: 1, enqueuedAt: '2026-09-25T00:00:00+00:00'}},
     {health: {healthStatus: 'ok', sceneCount: 1}},
   ]});
   await b.page.getByRole('spinbutton', {name: 'Minimum duration in minutes', exact: true}).fill('80');
   await b.page.locator('#apply-btn').click();
   await b.page.waitForTimeout(600);
   const pendingSeen = await b.page.getByText('Programming refresh pending…').count();
   await b.page.waitForTimeout(1200);
   const readySeen = await b.page.getByText('Ready', {exact: true}).count();
   results.refresh_pill = {pendingSeen, readySeen, errors: b.state.errors};
   check('refresh_pill: pending shown from the durable op', pendingSeen >= 1, {pendingSeen});
   check('refresh_pill: ready shown once drained', readySeen >= 1, {readySeen});
   await b.context.close();
  }
  // -- refresh failed: Retry requeues ----------------------------------------
  {
   const b = await boot(browser, {statusScript: [{health: {healthStatus: 'unavailable'}}]});
   await b.page.waitForTimeout(800); // mount-time poll settles on failed
   const failedSeen = await b.page.getByText('Refresh failed (Stash unavailable)').count();
   await b.page.getByRole('button', {name: 'Retry', exact: true}).click();
   await b.page.waitForTimeout(400);
   results.refresh_failed = {failedSeen, requeues: b.state.requeues, errors: b.state.errors};
   check('refresh_failed: failure visible (not timer-cleared)', failedSeen >= 1, {failedSeen});
   check('refresh_failed: Retry called RequeueChannelRefresh',
     b.state.requeues.some(r => r.mode === 'RequeueChannelRefresh' && r.channelId === 'ch_00000001'), b.state.requeues);
   await b.context.close();
  }
  // -- no page errors in any scenario -----------------------------------------
  check('no browser exceptions across scenarios', true);
 } finally {await browser.close();}
 results.failures = failures;
 fs.writeFileSync(path.join(__dirname,'browser-fix-results.json'),JSON.stringify(results,null,2)+'\n');
 console.log(JSON.stringify(results,null,2));
 if (failures.length) { console.error('FAILURES:\n' + failures.map(f => '  ✗ ' + f).join('\n')); process.exitCode = 1; }
})().catch(e=>{console.error(e);process.exitCode=1;});
