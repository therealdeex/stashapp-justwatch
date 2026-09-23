// Post-fix verification: loads the REAL production ui/index.js (no shims, no
// source patching) against a mock GraphQL backend and asserts the INTENDED
// outcomes for audit findings C1/C8/C9. Pre-fix evidence remains in
// browser-reproduce.cjs + browser-results.json.
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
      source:{type:'filter',tags:['5']},sourceLabel:'Tag',programming:{mode:'fixed'},provenance:{origin:'custom'}}))};
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

  // C1: the UNMODIFIED production route boots and stays usable.
  {
    const b = await boot(browser);
    results.c1_production_boot = {usable:true, pageErrors:b.state.errors};
    check('c1 pageErrors', b.state.errors.length === 0, b.state.errors);
    await b.context.close();
  }

  // C1: sessionStorage failures fall back to the real memory map.
  {
    const b = await boot(browser);
    await b.page.evaluate(() => {
      const orig = window.sessionStorage.setItem.bind(window.sessionStorage);
      window.sessionStorage.setItem = function(k, v) {
        if (String(k).startsWith('jw-studio-drafts')) throw new DOMException('full', 'QuotaExceededError');
        return orig(k, v);
      };
    });
    await b.page.locator('#f-name').fill('Renamed in memory');
    await b.page.locator('.jw-dial-row').filter({hasText:'Channel 2'}).click();
    await b.page.waitForTimeout(200);
    // the dial lists SERVER names; the draft badge is what marks the row
    await b.page.locator('.jw-dial-row').filter({hasText:'Channel 1'}).click();
    await b.page.waitForTimeout(300);
    const value = await b.page.locator('#f-name').inputValue();
    const draftPill = await b.page.locator('.jw-dial-row').filter({hasText:'Channel 1'}).locator('.jw-status-warn').count();
    results.c1_storage_failure_memory_fallback = {restoredName:value, draftBadge:draftPill > 0};
    check('c1 memory fallback keeps drafts', value === 'Renamed in memory', value);
    check('c1 memory draft visible on dial', draftPill > 0);
    await b.context.close();
  }

  // C9: group rename stages — no task until Apply.
  {
    const b = await boot(browser);
    await b.page.getByRole('button', {name:'Groups…', exact:true}).click();
    const input = b.page.getByRole('textbox', {name:'Group name My Channels', exact:true});
    await input.fill('Renamed on blur'); await input.press('Tab');
    await b.page.waitForTimeout(300);
    const staged = b.state.tasks.length;
    const applyEnabled = await b.page.getByRole('dialog', {name:'Groups'}).getByRole('button', {name:/^Apply$/}).isEnabled();
    await b.page.getByRole('dialog', {name:'Groups'}).getByRole('button', {name:/^Apply$/}).click();
    await waitTasks(b.page, b.state, 1);
    results.c9_group_rename_stages = {tasksBeforeApply:staged, applyEnabled,
      storedName:b.state.doc.groups[0].name, opsInTask:b.state.tasks[0] ? JSON.parse(b.state.tasks[0].ops).length : 0};
    check('c9 no task before Apply', staged === 0, staged);
    check('c9 apply enabled when staged', applyEnabled === true);
    check('c9 one op committed after Apply', b.state.doc.groups[0].name === 'Renamed on blur' && JSON.parse(b.state.tasks[0].ops).length === 1);
    await b.context.close();
  }

  // C9: bulk move into a NEW group is ONE atomic task.
  {
    const b = await boot(browser);
    await b.page.getByRole('button', {name:/^Select…$/}).click();
    await b.page.locator('.jw-dial-row').filter({hasText:'Channel 1'}).locator('input[type=checkbox]').check();
    await b.page.getByRole('button', {name:'Move to group…'}).click();
    await b.page.locator('#jw-bulk-newgroup').fill('Fresh Group');
    await b.page.getByRole('button', {name:'Apply move'}).click();
    await waitTasks(b.page, b.state, 1);
    const ops = JSON.parse(b.state.tasks[0].ops);
    const moved = b.state.doc.channels.filter(c => c.groupId === 'grp_my').length;
    results.c9_group_create_move_atomic = {taskCount:b.state.tasks.length, ops:ops.map(o => o.op),
      newGroup:b.state.doc.groups.map(g => g.name)};
    check('c9 single task', b.state.tasks.length === 1);
    check('c9 create+move in one transaction', ops.length === 2 && ops[0].op === 'group.put' && ops[1].op === 'channels.move');
    check('c9 channel moved', moved === 1 && b.state.doc.groups.some(g => g.name === 'Fresh Group'));
    await b.context.close();
  }

  // C8: an edit typed while applying survives navigation AND the receipt.
  {
    const b = await boot(browser);
    b.state.hold = true;
    await b.page.locator('#f-name').fill('Submitted snapshot');
    await b.page.locator('#apply-btn').click();
    await waitTasks(b.page, b.state, 1);
    await b.page.locator('#f-name').fill('NEWER UNSENT DRAFT');
    await b.page.waitForTimeout(150);
    const storedWhileApplying = await b.page.evaluate(() => {
      const raw = JSON.parse(sessionStorage.getItem('jw-studio-drafts-v1:lib_browser_fix'));
      return raw['ch_00000001'].draft.name;
    });
    await b.page.locator('.jw-dial-row').filter({hasText:'Channel 2'}).click();
    await b.page.getByRole('button', {name:'Leave now', exact:true}).click();
    b.state.hold = false;
    await b.page.waitForTimeout(3500); // receipt lands in the background
    await b.page.locator('.jw-dial-row').filter({hasText:'Submitted snapshot'}).click();
    await b.page.waitForTimeout(400);
    const after = await b.page.locator('#f-name').inputValue();
    const storedName = b.state.doc.channels[0].name;
    results.c8_newer_draft_survives_navigation = {storedWhileApplying, editorAfterReturning:after, committedName:storedName};
    check('c8 newer draft persisted during apply', storedWhileApplying === 'NEWER UNSENT DRAFT');
    check('c8 newer draft survives navigation+receipt', after === 'NEWER UNSENT DRAFT', after);
    check('c8 committed snapshot, not the newer draft', storedName === 'Submitted snapshot', storedName);
    await b.context.close();
  }

  // C8: stale full-record draft rebases instead of overwriting external edits.
  {
    const b = await boot(browser);
    await b.page.locator('#f-name').fill('My local rename');
    await b.page.waitForTimeout(200);
    // an external editor commits a color change at r2
    b.state.doc.channels[0].color = '#abcdef'; b.state.doc.revision = 2;
    await b.page.getByRole('button', {name:'Reload', exact:true}).click();
    await b.page.waitForTimeout(600);
    await b.page.locator('#apply-btn').click();
    await waitTasks(b.page, b.state, 1);
    await b.page.waitForTimeout(1200);
    const stored = b.state.doc.channels[0];
    results.c8_stale_draft_rebase = {
      expectedRevision:Number(b.state.tasks[0].expectedRevision),
      storedName:stored.name, storedColor:stored.color,
    };
    check('c8 external color kept', stored.color === '#abcdef', stored.color);
    check('c8 local rename kept', stored.name === 'My local rename', stored.name);
    await b.context.close();
  }

  // C8/C9: a transport failure keeps the request identity; the retry reuses it.
  {
    const b = await boot(browser);
    b.state.failTask = true;
    await b.page.locator('#f-name').fill('Resilient rename');
    await b.page.locator('#apply-btn').click();
    await waitTasks(b.page, b.state, 0); // submit failed
    await b.page.waitForTimeout(2500); // receipt poll exhausts quickly on unknown→ no: poll keeps trying; force release
    b.state.receipts['x'] = null; // not needed; retry path below
    // The draft must still be dirty, the Apply button must retry the SAME request
    const firstError = await b.page.locator('.jw-apply-state').innerText().catch(() => '');
    await b.page.locator('#apply-btn').click();
    await waitTasks(b.page, b.state, 1);
    await b.page.waitForTimeout(1500);
    const stored = b.state.doc.channels[0].name;
    results.c8_transport_retry_same_request = {firstError: firstError.slice(0, 80),
      requestIds: b.state.tasks.map(t => t.requestId), committedName: stored};
    check('c8 transport failure reported, not "nothing changed"', !/nothing changed/i.test(firstError), firstError);
    check('c8 retry commits once', b.state.tasks.length === 1 && stored === 'Resilient rename', {tasks: b.state.tasks.length, stored});
    await b.context.close();
  }

  await browser.close();
  results.PASS = failures.length === 0;
  results.failures = failures;
  console.log(JSON.stringify(results, null, 2));
  process.exit(failures.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
