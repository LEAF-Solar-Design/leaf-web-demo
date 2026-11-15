// Native-browser focus replay for saved tracking. Synthetic transport only.
// Run from web: node e2e/s02-focus/inspection-focus.mjs --case keyboard --out <absolute directory>
// Optional: --source-root <web directory> --browser-executable <installed Chromium>.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createServer as createHttpServer } from 'node:http'
import { mkdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { chromium } from 'file:///C:/GitHub/leaf-web-demo/web/node_modules/%40playwright/test/index.mjs'

const args = process.argv.slice(2)
const option = name => { const index = args.indexOf(name); return index < 0 ? null : args[index + 1] }
assert(option('--out'), '--out is required')
let out = resolve(option('--out'))
const root = realpathSync(option('--source-root') || fileURLToPath(new URL('../../', import.meta.url)))
mkdirSync(out, { recursive: true })
out = realpathSync(out)
// Windows checkouts may be reached through a drive alias. Esbuild's output
// metadata and Vite's cache lookup must use the same canonical working root.
process.chdir(root)
const fixture = `
import React from 'react';import{createRoot}from'react-dom/client';
import Panel from '/src/solar/ProjectSolarReadPanel.jsx';
let B={kind:'project',organization_id:'11111111-1111-4111-8111-111111111111',project_id:'22222222-2222-4222-8222-222222222222',drawing_id:'33333333-3333-4333-8333-333333333333',input_version_id:'44444444-4444-4444-8444-444444444444'};
const D='sha256:'+'c'.repeat(64),name='solar-solaredge-tracking-read',unused=0;let snapshot={binding:B,authorityKey:'synthetic-event-proof'};const listeners=new Set();
const refs=Array.from({length:1234},(_,i)=>'leaf:panel:00000000-0000-4000-8000-'+String(i).padStart(12,'0'));
globalThis.__s02Requests=[];globalThis.__pending=[];globalThis.__delay=false;
const client={getSnapshot:()=>snapshot,captureCatalogScope:()=>()=>true,subscribe:fn=>{listeners.add(fn);return()=>listeners.delete(fn)},downloadArtifact:()=>{throw Error('not a download test')},readSolar:async({params})=>{
__s02Requests.push(JSON.parse(JSON.stringify(params)));const offset=params.offset||0,items=refs.slice(offset,offset+1000).map(ref=>B.input_version_id.startsWith('5555')?'report-B:'+ref:ref),end=offset+items.length;
const output={schema:'leaf.solar-project-solaredge-tracking-read.v1',accepted:true,labels_only:true,section:'unassigned',page:{offset,returned:items.length,total:refs.length,next_offset:end<refs.length?end:null},items,acceptance:{mode:'matched-only',status:'partial',unresolved_groups:1},one_liner:'Synthetic labels only. Remaining panels need correction.'};
const response={ok:true,status:200,data:{result:{schema:'leaf.solar-project-read.v1',adapter:'project-local-graph-read',organization_id:B.organization_id,project_id:B.project_id,drawing_id:B.drawing_id,input_version_id:B.input_version_id,input_intake_sha256:'a'.repeat(64),graph_sha256:'b'.repeat(64),tool:name,tool_manifest_sha256:D,graph_project_id:'synthetic',head_version_id:B.input_version_id,is_head:true,current_required:false,request_sha256:'d'.repeat(64),output_sha256:'e'.repeat(64),output_bytes:JSON.stringify(output).length,drawing_changed:false,output}}};if(!globalThis.__delay)return response;return new Promise(resolve=>__pending.push({params:JSON.parse(JSON.stringify(params)),resolve,response}));}};
const tool={name,catalog_digest:D,availability:{runnable:true},params:{type:'object',properties:{offset:{type:'integer',minimum:0,default:0},section:{type:'string',enum:['unassigned'],default:'unassigned'}}}};
const catalog={binding:B,authorityKey:snapshot.authorityKey,client,families:[{capabilities:[tool]}],isCurrent:()=>true};
const root=createRoot(document.getElementById('root'));globalThis.__mount=()=>root.render(<Panel tool={tool} solarBinding={B} authorityKey={snapshot.authorityKey} projectClient={client} projectCatalog={catalog} isCurrent={()=>true}/>);__mount();globalThis.__switchVersion=()=>{B={...B,input_version_id:'55555555-5555-4555-8555-555555555555'};snapshot={binding:B,authorityKey:'synthetic-event-proof-B'};catalog.binding=B;catalog.authorityKey=snapshot.authorityKey;for(const fn of listeners)fn();__mount()};
`
const html = '<!doctype html><meta charset="utf-8"><title>S02 synthetic Offset regression</title><style>body{font:16px system-ui;margin:24px}table{max-width:100%}td{overflow-wrap:anywhere}button,input,select{font:inherit;margin:6px}</style><div id="root"></div><script type="module" src="/s02-offset-fixture.jsx"></script>'
const report = { parent: 'Studio18 S02 saved tracking continuation',
  mode: 'real component/native browser with synthetic transport; not LIVE, real auth or a full App',
  source_root: root, source_sha256: createHash('sha256').update(readFileSync(resolve(root, 'src/solar/ProjectSolarReadPanel.jsx'))).digest('hex'),
  stage: 'starting' }
let vite, browser
try {
  const reservation = createHttpServer()
  await new Promise((resolve, reject) => { reservation.once('error', reject); reservation.listen(0, '127.0.0.1', resolve) })
  const port = reservation.address().port
  await new Promise(resolve => reservation.close(resolve))
  vite = await createServer({ configFile: false, envFile: false, root,
    cacheDir: resolve(out, '.vite-cache'), resolve: { dedupe: ['react', 'react-dom'] }, esbuild: { jsx: 'automatic' },
    optimizeDeps: { noDiscovery: true, include: ['react', 'react-dom/client', 'react/jsx-runtime', 'react/jsx-dev-runtime'] },
    server: { host: '127.0.0.1', port, strictPort: true, hmr: false }, plugins: [{ name: 's02-offset-fixture',
      resolveId(id) { if (id === '/s02-offset-fixture.jsx') return id },
      load(id) { if (id === '/s02-offset-fixture.jsx') return fixture },
      configureServer(server) { server.middlewares.use((req, res, next) => {
        if (req.url === '/s02-offset') { res.setHeader('Content-Type', 'text/html'); res.end(html) } else next()
      }) } }] })
  await vite.listen()
  browser = await chromium.launch({ headless: true, ...(option('--browser-executable') ? { executablePath: option('--browser-executable') } : {}) })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  page.setDefaultTimeout(10_000)
  await page.goto(`http://127.0.0.1:${port}/s02-offset`, { waitUntil: 'networkidle' })

  const mode = option('--case') || 'keyboard'
  report.case = mode
  const focus = async () => page.evaluate(() => ({tag:document.activeElement?.tagName,id:document.activeElement?.id,text:(document.activeElement?.innerText||document.activeElement?.getAttribute('aria-label')||'').slice(0,100)}))
  const read=page.getByRole('button',{name:'Read project output',exact:true})
  const offset=page.getByRole('spinbutton',{name:'Offset',exact:true})
  const list=page.getByRole('region',{name:'Unassigned panel references',exact:true})
  if(mode==='keyboard') {
    report.initial_tab_order=[]
    for(let i=0;i<10;i++) {
      await page.keyboard.press('Tab');const current=await focus();report.initial_tab_order.push(current)
      if(current.text==='Read project output')break
    }
    assert.equal((await focus()).text,'Read project output')
    await page.keyboard.press('Enter');await list.waitFor()
    await page.evaluate(()=>{
      globalThis.__announcements=[]
      globalThis.__liveObserver=new MutationObserver(()=>{const el=document.querySelector('[aria-label="Unassigned panel references"] [aria-live]');if(el)__announcements.push(el.textContent)})
      __liveObserver.observe(document.getElementById('root'),{subtree:true,childList:true,characterData:true})
    })
    await page.screenshot({path:resolve(out,'01-keyboard-first-page.png')})
    report.after_read_tab_order=[]
    for(let i=0;i<10;i++) {
      await page.keyboard.press('Tab');const current=await focus();report.after_read_tab_order.push(current)
      if(current.text==='Next references')break
    }
    assert.equal((await focus()).text,'Next references')
    await page.keyboard.press('Enter')
    await page.waitForFunction(()=>document.querySelector('[aria-label="Unassigned panel references"] [aria-live]')?.textContent.includes('101 to 200'))
    report.focus_after_page_change=await focus()
    for(let i=0;i<8;i++)await page.keyboard.press('Enter')
    await page.waitForFunction(()=>document.querySelector('[aria-label="Unassigned panel references"] [aria-live]')?.textContent.includes('901 to 1000'))
    report.last_page_focus=await focus()
    report.last_page_next_disabled=await page.getByRole('button',{name:'Next references',exact:true}).isDisabled()
    assert.equal((await focus()).text,'Previous references')
    report.focus_after_shift_tab=await focus()
    await page.keyboard.press('Enter')
    await page.waitForFunction(()=>document.querySelector('[aria-label="Unassigned panel references"] [aria-live]')?.textContent.includes('801 to 900'))
    report.announcements=await page.evaluate(()=>__announcements)
    assert(report.announcements.some(s=>s.includes('101 to 200')))
    assert(report.announcements.some(s=>s.includes('901 to 1000')))
    report.narrow=[]
    await page.setViewportSize({width:390,height:844})
    await page.keyboard.press('Tab')
    await page.screenshot({path:resolve(out,'02-keyboard-narrow-page.png')})
    report.narrow.push(await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth})))
    assert(report.narrow.every(s=>s.document<=s.viewport&&s.body<=s.viewport))
    await page.keyboard.press('Control+Home')
    await page.screenshot({path:resolve(out,'03-keyboard-narrow-heading.png')})
    report.keyboard_only_actions=true
    report.actual_screen_reader_audio_tested=false
    await page.evaluate(()=>__liveObserver.disconnect())
  } else if(mode==='response-order') {
    await page.evaluate(()=>{__delay=true})
    await read.click();await page.waitForFunction(()=>__pending.length===1)
    await offset.fill('1000');await read.click();await page.waitForFunction(()=>__pending.length===2)
    await page.evaluate(()=>__pending[1].resolve(__pending[1].response));await list.waitFor()
    assert((await list.innerText()).includes('1001 to 1100'))
    const fresh=await list.innerText()
    await page.evaluate(()=>__pending[0].resolve(__pending[0].response))
    await page.waitForTimeout(40)
    assert.equal(await list.innerText(),fresh)
    await page.screenshot({path:resolve(out,'01-reversed-response-kept.png')})
    await offset.fill('0');await read.click();await page.waitForFunction(()=>__pending.length===3)
    await page.evaluate(()=>__pending[2].resolve({ok:false,status:409,reason:'SOLAREDGE_IMPORT_RECORD_INVALID'}))
    await page.getByRole('status').filter({hasText:'Loading'}).waitFor({state:'hidden'}).catch(()=>{})
    assert.equal(await list.count(),0)
    await page.waitForFunction(()=>document.querySelector('button[type=submit]')?.disabled===false)
    report.focus_after_failure=await focus();assert.equal(report.focus_after_failure.text,'Read project output')
    await page.keyboard.press('Enter');await page.waitForFunction(()=>__pending.length===4)
    await page.evaluate(()=>__pending[3].resolve(__pending[3].response));await list.waitFor()
    assert((await list.innerText()).includes('1 to 100'))
    assert(!(await list.innerText()).includes('1001 to 1100'))
    await page.screenshot({path:resolve(out,'02-failed-read-retry-page-zero.png')})
    await read.click();await page.waitForFunction(()=>__pending.length===5)
    await page.evaluate(()=>__switchVersion());await list.waitFor({state:'hidden'})
    await read.click();await page.waitForFunction(()=>__pending.length===6)
    await page.evaluate(()=>__pending[5].resolve(__pending[5].response));await list.waitFor()
    assert((await list.innerText()).includes('report-B:'))
    await page.evaluate(()=>__pending[4].resolve(__pending[4].response))
    await page.waitForTimeout(40)
    assert((await list.innerText()).includes('report-B:'))
    assert.equal(await list.locator('tbody tr').count(),100)
    report.new_saved_version_after_late_prior_report=true
    report.reversed_old_response_ignored=true
    report.failed_read_cleared_prior_rows=true
    report.retry_offset_and_rows_consistent=true
    report.limitation='Two synthetic version-bound reports over a controlled transport that deliberately ignores AbortSignal. Real auth/storage and full App are not exercised.'
  } else throw Error('unknown case '+mode)
  report.requests=await page.evaluate(()=>__s02Requests)
  report.stage = 'PASS'
} catch (error) { report.stage = 'FAIL'; report.reason = String(error.message).slice(0, 600); report.stack = error.stack; process.exitCode = 1 }
finally {
  if (browser) { await browser.close(); report.browser_closed = true }
  if (vite) { await vite.close(); report.vite_closed = true }
  writeFileSync(resolve(out, 'receipt.json'), JSON.stringify(report, null, 2) + '\n')
  console.log(JSON.stringify(report))
}
process.exit(process.exitCode || 0)
