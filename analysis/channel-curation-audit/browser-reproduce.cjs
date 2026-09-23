// First boots unmodified production UI. Subsequent interaction probes add ONLY
// the missing draft-store count() method to an in-memory source copy.
// No connection to Stash or any device. Dependency instructions in audit report.
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
  return {libraryId:'lib_browser_audit',revision:1,
    groups:[{id:'grp_my',name:'My Channels',position:1}],
    channels:[1,2].map(i=>({id:`ch_${String(i).padStart(8,'0')}`,kind:'ch',number:i,name:`Channel ${i}`,
      glyph:null,color:'#112233',groupId:'grp_my',sort:'shuffle',seed:i,enabled:true,archived:false,paused:false,
      source:{type:'filter',tags:['5']},sourceLabel:'Tag',programming:{mode:'fixed'},provenance:{origin:'custom'}}))};
}
async function boot(browser, shim = true) {
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await context.newPage();
  const state={doc:fixture(),tasks:[],receipts:{},hold:false,errors:[]};
  page.on('pageerror',e=>state.errors.push(e.message));
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/graphql') {
      const body=route.request().postDataJSON(), vars=body.variables||{};
      let data={};
      if(body.query.includes('runPluginTask')) {
        const args=vars.a, ops=JSON.parse(args.ops);
        state.tasks.push(clone(args));
        if(Number(args.expectedRevision)!==state.doc.revision) {
          state.receipts[args.requestId]={requestId:args.requestId,status:'rejected',error:'revision_conflict',currentRevision:state.doc.revision};
        } else {
          for(const op of ops) {
            if(op.op==='channel.put')state.doc.channels=state.doc.channels.map(c=>c.id===op.channel.id?clone(op.channel):c);
            if(op.op==='group.put')state.doc.groups=state.doc.groups.map(g=>g.id===op.group.id?clone(op.group):g);
          }
          state.doc.revision++;
          state.receipts[args.requestId]={requestId:args.requestId,status:'committed',revision:state.doc.revision};
        }
        data={runPluginTask:'job-audit'};
      } else if(body.query.includes('runPluginOperation')) {
        const a=vars.args;
        let out;
        switch(a.mode) {
          case 'GetChannelLibrary': out={...clone(state.doc),total:state.doc.channels.length};break;
          case 'GetChannelDefinition':out={revision:state.doc.revision,channel:clone(state.doc.channels.find(c=>c.id===a.channelId)),summary:[]};break;
          case 'PreviewChannelPool':out={status:'ok',poolCount:1,rotationSize:1,sample:[],signature:'mock'};break;
          case 'ValidateChannelChanges':out={valid:true,errors:[],effects:[],revision:state.doc.revision};break;
          case 'GetChannelApplyResult':out=state.hold?{status:'unknown'}:state.receipts[a.requestId]||{status:'unknown'};break;
          default:out={};
        }
        data={runPluginOperation:out};
      } else data={findTag:{name:'Tag 5'},findPerformer:{name:'Performer'},findStudio:{name:'Studio'}};
      return route.fulfill({contentType:'application/json',body:JSON.stringify({data})});
    }
    if(scripts[url.pathname]) { let content=scripts[url.pathname]; if(url.pathname==='/ui.js' && shim) content=content.toString().replace('ids() { return Object.keys(readAll()); },', 'ids() { return Object.keys(readAll()); }, count() { return Object.keys(readAll()).length; },'); return route.fulfill({contentType:url.pathname.endsWith('.css')?'text/css':'text/javascript',body:content}); }
    return route.fulfill({contentType:'text/html',body:html});
  });
  await page.goto('http://audit.invalid/');
  try { if(shim) await page.locator('#f-name').waitFor({timeout:10000}); else await page.getByRole('alert').waitFor({timeout:10000}); } catch(e) { console.error(JSON.stringify({pageErrors:state.errors,body:await page.locator('body').innerText()})); await context.close(); throw e; }
  return {context,page,state};
}
async function waitTasks(page,state,count){await page.waitForTimeout(100);for(let i=0;i<40 && state.tasks.length<count;i++)await page.waitForTimeout(100);if(state.tasks.length<count)throw Error('task not submitted')}
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.JW_AUDIT_CHROME||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
  const results={}; const original=await boot(browser,false); results.production_boot={shim:false,body:await original.page.locator('body').innerText(),pageErrors:original.state.errors}; await original.context.close(); results.interaction_probe_notice='Remaining probes add only count() to the in-memory draft-store copy; product files unchanged.';
  async function run(name,test){const b=await boot(browser);try{results[name]=await test(b)}catch(e){results[name]={error:e.message}}results[name].pageErrors=b.state.errors;await b.context.close();}
  await run('group_rename_without_apply',async({page,state})=>{
    await page.getByRole('button',{name:'Groups…',exact:true}).click();
    const input=page.getByRole('textbox',{name:'Group name My Channels',exact:true});
    await input.fill('Renamed on blur');await input.press('Tab');
    await waitTasks(page,state,1);
    return {clickedApply:false,taskCount:state.tasks.length,storedGroupName:state.doc.groups[0].name};
  });
  await run('stale_draft_overwrites_external_edit',async({page,state})=>{
    await page.locator('#f-name').fill('My local rename');
    state.doc.channels[0].color='#abcdef';state.doc.revision=2;
    await page.getByRole('button',{name:'Reload',exact:true}).click();
    await page.waitForTimeout(250);
    await page.locator('#apply-btn').click();await waitTasks(page,state,1);
    await page.waitForTimeout(1000);
    return {externallySavedColor:'#abcdef',finalStoredColor:state.doc.channels[0].color,
      expectedRevision:Number(state.tasks[0].expectedRevision),taskCount:state.tasks.length,finalName:state.doc.channels[0].name};
  });
  await run('newer_inflight_edit_lost_after_navigation',async({page,state})=>{
    state.hold=true;
    await page.locator('#f-name').fill('Submitted snapshot');
    await page.locator('#apply-btn').click();await waitTasks(page,state,1);
    await page.locator('#f-name').fill('NEWER UNSENT DRAFT');
    const localBefore=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('jw-studio-drafts-v1:lib_browser_audit')));
    await page.locator('.jw-dial-row').filter({hasText:'Channel 2'}).click();
    await page.getByRole('button',{name:'Leave now',exact:true}).click();
    state.hold=false;await page.waitForTimeout(3000);
    await page.locator('.jw-dial-row').filter({hasText:'Submitted snapshot'}).click();
    await page.waitForTimeout(250);
    return {typedNewerDraft:'NEWER UNSENT DRAFT',sessionDraftWhileApplying:localBefore['ch_00000001'].draft.name,
      editorAfterReturning:await page.locator('#f-name').inputValue(),storedName:state.doc.channels[0].name};
  });
  await run('newer_inflight_edit_lost_even_without_leaving_until_receipt',async({page,state})=>{
    state.hold=true;
    await page.locator('#f-name').fill('Submitted snapshot');await page.locator('#apply-btn').click();await waitTasks(page,state,1);
    await page.locator('#f-name').fill('NEWER UNSENT DRAFT');state.hold=false;await page.waitForTimeout(3000);
    const beforeLeaving=await page.locator('#f-name').inputValue();
    await page.locator('.jw-dial-row').filter({hasText:'Channel 2'}).click();
    await page.waitForTimeout(100);
    await page.locator('.jw-dial-row').filter({hasText:'Submitted snapshot'}).click();
    await page.waitForTimeout(150);
    return {editorAfterReceipt:beforeLeaving,editorAfterNavigation:await page.locator('#f-name').inputValue()};
  });
  await browser.close();
  console.log(JSON.stringify(results,null,2));
})().catch(e=>{console.error(e);process.exit(1)});
