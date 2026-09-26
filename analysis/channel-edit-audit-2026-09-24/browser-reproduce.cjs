// Audit probes: unmodified production ui/index.js with synthetic fixtures.
// Adapted from the previous browser audit harness. GraphQL is mocked; emitted
// payloads are checked separately by reproduce.py against actual Python validation.
const fs = require('fs');
const path = require('path');
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
  const state = {doc:fixture(),tasks:[],receipts:{},hold:false,failTask:false,errors:[]};
  page.on('pageerror', e => state.errors.push(e.message));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/graphql') {
      const body = route.request().postDataJSON(), vars = body.variables || {};
      let data = {};
      if (body.query.includes('runPluginTask')) {
        if (state.failTask) { state.failTask = false; return route.fulfill({contentType:'application/json',body:JSON.stringify({errors:[{message:'network blip'}]})}); }
        const args = vars.a, ops = JSON.parse(args.ops);
        state.tasks.push(clone(args));
        if (Number(args.expectedRevision) !== state.doc.revision) {
          state.receipts[args.requestId] = {requestId:args.requestId,status:'rejected',error:'revision_conflict',currentRevision:state.doc.revision};
        } else {
          const newIds = {};
          for (const op of ops) {
            if (op.op === 'channel.put') state.doc.channels = state.doc.channels.map(c => c.id === op.channel.id ? clone(op.channel) : c);
            if (op.op === 'channel.create') {
              const id = 'ch_' + String(state.doc.channels.length + 1).padStart(8, '0');
              newIds[op.tempId] = id;
              state.doc.channels.push(Object.assign({}, clone(op.channel), {id, seed:77}));
            }
            if (op.op === 'group.put') {
              const exists = state.doc.groups.some(g => g.id === op.group.id);
              if (!exists) state.doc.groups.push(clone(op.group));
              else state.doc.groups = state.doc.groups.map(g => g.id === op.group.id ? clone(op.group) : g);
            }
            if (op.op === 'group.delete') {
              state.doc.channels.forEach(c => { if (c.groupId === op.id && op.moveTo) c.groupId = op.moveTo; });
              state.doc.groups = state.doc.groups.filter(g => g.id !== op.id);
            }
            if (op.op === 'channels.move') state.doc.channels.forEach(c => { if (op.channelIds.includes(c.id)) c.groupId = op.groupId; });
          }
          state.doc.revision++;
          state.receipts[args.requestId] = {requestId:args.requestId,status:'committed',revision:state.doc.revision,
            ...(Object.keys(newIds).length ? {idMap:newIds} : {})};
        }
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
          case 'PreviewChannelPool': out = {status:'ok',poolCount:1,rotationSize:1,sample:[],signature:'mock'}; break;
          case 'ValidateChannelChanges': out = {valid:true,errors:[],effects:[],revision:state.doc.revision}; break;
          case 'GetChannelApplyResult': out = state.hold ? {status:'unknown'} : (state.receipts[a.requestId] || {status:'unknown'}); break;
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

(async () => {
 const browser = await chromium.launch({executablePath:process.env.JW_AUDIT_CHROME||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const results = {};
 try {
  {
   const b=await boot(browser);
   await b.page.getByRole('spinbutton',{name:'Minimum duration in minutes',exact:true}).fill('80');
   await b.page.locator('#apply-btn').click();
   await waitTasks(b.page,b.state,1);
   await b.page.waitForTimeout(1200);
   results.duration_60_to_80={source:JSON.parse(b.state.tasks[0].ops)[0].channel.source,errors:b.state.errors,applyState:await b.page.locator('.jw-apply-state').innerText()};
   await b.context.close();
  }
  {
   const b=await boot(browser);
   await b.page.getByRole('button',{name:'Remove Tag 5',exact:true}).click();
   await b.page.getByRole('spinbutton',{name:'Minimum duration in minutes',exact:true}).fill('80');
   await b.page.locator('#apply-btn').click();
   await waitTasks(b.page,b.state,1);
   results.remove_tag_then_duration={source:JSON.parse(b.state.tasks[0].ops)[0].channel.source,errors:b.state.errors};
   await b.context.close();
  }
  {
   const b=await boot(browser);
   await b.page.locator('#f-name').fill('Renamed');
   await b.page.getByRole('textbox',{name:'Text search',exact:true}).fill('new query');
   await b.page.locator('#apply-btn').click();
   await waitTasks(b.page,b.state,1);
   await b.page.waitForTimeout(1200);
   results.immediate_text_apply={source:JSON.parse(b.state.tasks[0].ops)[0].channel.source,errors:b.state.errors,displayedQuery:await b.page.getByRole('textbox',{name:'Text search',exact:true}).inputValue(),applyState:await b.page.locator('.jw-apply-state').innerText()};
   await b.context.close();
  }
 } finally {await browser.close();}
 fs.writeFileSync(path.join(__dirname,'browser-results.json'),JSON.stringify(results,null,2)+'\n');
 console.log(JSON.stringify(results,null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
