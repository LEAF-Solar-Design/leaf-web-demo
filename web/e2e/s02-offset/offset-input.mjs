// Native-browser regression for the real project read form. Synthetic transport only.
// Run from web: node e2e/s02-offset/offset-input.mjs --out <absolute directory>
// Optional: --source-root <web directory> --browser-executable <installed Chromium>.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createServer as createHttpServer } from 'node:http'
import { mkdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { chromium } from '@playwright/test'

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
const B={kind:'project',organization_id:'11111111-1111-4111-8111-111111111111',project_id:'22222222-2222-4222-8222-222222222222',drawing_id:'33333333-3333-4333-8333-333333333333',input_version_id:'44444444-4444-4444-8444-444444444444'};
const D='sha256:'+'c'.repeat(64),name='solar-solaredge-tracking-read',snapshot={binding:B,authorityKey:'synthetic-event-proof'};
const refs=Array.from({length:1234},(_,i)=>'leaf:panel:00000000-0000-4000-8000-'+String(i).padStart(12,'0'));
globalThis.__s02Requests=[];
const client={getSnapshot:()=>snapshot,captureCatalogScope:()=>()=>true,subscribe:()=>()=>{},downloadArtifact:()=>{throw Error('not a download test')},readSolar:async({params})=>{
__s02Requests.push(JSON.parse(JSON.stringify(params)));const offset=params.offset||0,items=refs.slice(offset,offset+1000),end=offset+items.length;
const output={schema:'leaf.solar-project-solaredge-tracking-read.v1',accepted:true,labels_only:true,section:'unassigned',page:{offset,returned:items.length,total:refs.length,next_offset:end<refs.length?end:null},items,acceptance:{mode:'matched-only',status:'partial',unresolved_groups:1},one_liner:'Synthetic labels only. Remaining panels need correction.'};
return{ok:true,status:200,data:{result:{schema:'leaf.solar-project-read.v1',adapter:'project-local-graph-read',organization_id:B.organization_id,project_id:B.project_id,drawing_id:B.drawing_id,input_version_id:B.input_version_id,input_intake_sha256:'a'.repeat(64),graph_sha256:'b'.repeat(64),tool:name,tool_manifest_sha256:D,graph_project_id:'synthetic',head_version_id:B.input_version_id,is_head:true,current_required:false,request_sha256:'d'.repeat(64),output_sha256:'e'.repeat(64),output_bytes:JSON.stringify(output).length,drawing_changed:false,output}}};}};
const tool={name,catalog_digest:D,availability:{runnable:true},params:{type:'object',properties:{offset:{type:'integer',minimum:0,default:0},section:{type:'string',enum:['unassigned'],default:'unassigned'}}}};
const catalog={binding:B,authorityKey:snapshot.authorityKey,client,families:[{capabilities:[tool]}],isCurrent:()=>true};
createRoot(document.getElementById('root')).render(<Panel tool={tool} solarBinding={B} authorityKey={snapshot.authorityKey} projectClient={client} projectCatalog={catalog} isCurrent={()=>true}/>);
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
  const offset = page.getByRole('spinbutton', { name: 'Offset', exact: true })
  const read = page.getByRole('button', { name: 'Read project output', exact: true })
  await offset.fill('1000')
  assert.equal(await offset.inputValue(), '1000', 'native input must retain the edited numeric value')
  await read.click(); await page.waitForFunction(() => globalThis.__s02Requests.length === 1)
  assert.equal(await page.evaluate(() => __s02Requests[0].offset), 1000)
  await offset.fill('1001'); assert.equal(await offset.inputValue(), '1001')
  await read.click(); await page.waitForFunction(() => globalThis.__s02Requests.length === 2)
  assert.equal(await page.evaluate(() => __s02Requests[1].offset), 1001)
  // Attempt a stale submission during capture, before the value callback commits.
  await page.evaluate(() => {
    const input = document.getElementById('sf-offset'), form = input.form
    form.addEventListener('input', () => form.requestSubmit(), { capture: true, once: true })
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '1002')
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: '1002' }))
  })
  assert.equal(await page.evaluate(() => __s02Requests.length), 2, 'capture must reject the stale same-event submission')
  assert.equal(await offset.inputValue(), '1002')
  await read.click(); await page.waitForFunction(() => globalThis.__s02Requests.length === 3)
  assert.equal(await page.evaluate(() => __s02Requests[2].offset), 1002)
  await offset.press('Control+A'); await offset.pressSequentially('1000')
  assert.equal(await offset.inputValue(), '1000')
  await page.screenshot({ path: resolve(out, 'offset-kept.png') })
  report.requests = await page.evaluate(() => __s02Requests)
  report.native_input_and_keyboard_kept = true
  report.same_event_stale_submit_refused = true
  report.stage = 'PASS'
} catch (error) { report.stage = 'FAIL'; report.reason = String(error.message).slice(0, 600); report.stack = error.stack; process.exitCode = 1 }
finally {
  if (browser) { await browser.close(); report.browser_closed = true }
  if (vite) { await vite.close(); report.vite_closed = true }
  writeFileSync(resolve(out, 'receipt.json'), JSON.stringify(report, null, 2) + '\n')
  console.log(JSON.stringify(report))
}
process.exit(process.exitCode || 0)
