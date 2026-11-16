// Actual JSX -> local JSON stdin/stdout -> Python WorkflowAdapter -> SQLite.
// No browser/network and no mocked confirmation/apply persistence callbacks.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os')
const Module = require('node:module'), { spawn } = require('node:child_process')
const assert = require('node:assert/strict')
const at = process.argv.indexOf('--deps'), deps = path.resolve(at < 0 ? 'web/node_modules' : process.argv[at + 1])
const dependency = name => require(path.join(deps, name))
const { JSDOM } = dependency('jsdom')
const dom = new JSDOM('<html><body></body></html>', { url: 'http://localhost' })
global.window = dom.window; global.document = dom.window.document
Object.defineProperty(global, 'navigator', { configurable: true, value: dom.window.navigator })
global.HTMLElement = dom.window.HTMLElement; global.MutationObserver = dom.window.MutationObserver
global.fetch = dom.window.fetch = () => { throw new Error('Offline JSON journey forbids network') }
const React = dependency('react'), { render, screen, fireEvent, cleanup, waitFor } = dependency('@testing-library/react')
const { transformSync } = dependency('esbuild'), cache = new Map()
function component(name) {
  if (cache.has(name)) return cache.get(name)
  const file = path.resolve(__dirname, '../../../web/src/solar/' + name + '.jsx')
  const compiled = new Module(file, module); compiled.filename = file; compiled.paths = [deps]
  const nativeRequire = compiled.require.bind(compiled)
  compiled.require = request => request.startsWith('./X07') ? { default: component(request.slice(2)), __esModule: true } : nativeRequire(request)
  compiled._compile(transformSync(fs.readFileSync(file, 'utf8'), { loader: 'jsx', format: 'cjs', jsx: 'automatic' }).code, file)
  cache.set(name, compiled.exports.default); return compiled.exports.default
}
const Workflow = component('X07ProfileWorkflow')
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'x07-json-journey-'))
const database = path.join(root, 'profiles.sqlite')
const python = `
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter
request=json.load(sys.stdin)
try:
    store=CustomerProfileStore(request['database'])
    adapter=WorkflowAdapter(ProfileWorkflow(store),context=request['bound_context'],intake=request['intake'],copies=request['copies'])
    op=request['op']; value=request.get('value'); context=request['context']
    if op=='load': result=adapter.load_workflow(context)
    elif op=='confirm': result=adapter.confirm_roles(context,value)
    elif op=='apply': result=adapter.apply_profile(context,value)
    elif op=='editor_load': result=adapter.load_profile(value)
    elif op=='editor_save': result=adapter.save_profile(value['customer'],value['roles'],expected_version=value['expected_version'])
    else: raise ValueError('unknown offline operation')
    print(json.dumps({'ok':True,'result':result},ensure_ascii=False))
except Exception as error:
    print(json.dumps({'ok':False,'error':{'type':type(error).__name__,'message':str(error),'code':getattr(error,'code',None)}}))
`
const intake = { inserts: [0, 1, 2, 10, 11, 12].map(x => ({ name: 'Panel', layer: 'MODULES', x, y: 0 })) }
const copies = [0, 10].map(x => ({ block: 'Panel', layer: 'MODULES', design_window: [x, 0, x + 2, 0], oracle_count: 3 }))
const ctx = (project, drawing = project + ':v1') => ({ customer_key: 'synthetic-a', project_key: project, drawing_key: drawing })
const calls = [], children = new Set()
function rpc(bound, op, context, value, signal) {
  calls.push({ op, context, value })
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new Error('retired')); return }
    const child = spawn('python', ['-X', 'utf8', '-c', python], { cwd: path.resolve(__dirname, '../../..'), stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
    children.add(child)
    let stdout = '', stderr = '', finished = false
    const timer = setTimeout(() => { child.kill(); }, 15000)
    const abort = () => child.kill()
    signal?.addEventListener('abort', abort, { once: true })
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8')
    child.stdout.on('data', chunk => { stdout += chunk }); child.stderr.on('data', chunk => { stderr += chunk })
    const settle = (error, result) => {
      if (finished) return
      finished = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); children.delete(child)
      error ? reject(error) : resolve(result)
    }
    child.on('error', error => settle(error))
    child.stdin.on('error', error => settle(error))
    child.on('close', code => {
      if (code !== 0) { settle(new Error('Python JSON worker failed: ' + stderr.slice(0, 500))); return }
      try {
        const response = JSON.parse(stdout)
        if (!response.ok) { const error = new Error(response.error.message); error.code = response.error.code; settle(error) }
        else settle(null, response.result)
      } catch (error) { settle(error) }
    })
    child.stdin.end(JSON.stringify({ database, bound_context: bound, context, op, value, intake, copies }))
  })
}
function mount(context) {
  return render(React.createElement(Workflow, { customerKey: context.customer_key,
    projectKey: context.project_key, drawingKey: context.drawing_key,
    loadWorkflow: (scope, opts) => rpc(context, 'load', scope, null, opts.signal),
    confirmRoles: (scope, value, opts) => rpc(context, 'confirm', scope, value, opts.signal),
    applyProfile: (scope, value, opts) => rpc(context, 'apply', scope, value, opts.signal),
    loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
    saveProfile: (customer, roles, opts) => rpc(context, 'editor_save', context,
      { customer, roles, expected_version: opts.expected_version }, opts.signal) }))
}
async function applied() {
  await screen.findByText('Drawing profile applied.', {}, { timeout: 15000 })
  await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
  await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled), { timeout: 15000 })
}
async function main() {
  try {
    const first = ctx('first')
    mount(first)
    await screen.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByRole('button', { name: 'Use copy 2' })
    assert.equal(calls.filter(row => row.op === 'confirm').length, 0)
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 2' }))
    await applied()
    let pin = await rpc(first, 'load', first)
    assert.equal(pin.application_record.version, 1)
    assert.equal(calls.filter(row => row.op === 'confirm').length, 1)
    assert.deepEqual(calls.find(row => row.op === 'confirm').value.design_window, [10, 0, 12, 0])
    // An explicit editor save creates version2; the rendered old pin stays1.
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.ok(screen.getByText('Project is pinned to version 1.'))
    cleanup()

    const next = ctx('next')
    mount(next)
    await screen.findByRole('button', { name: 'Use copy 1' }, { timeout: 15000 })
    assert.equal(screen.queryByRole('button', { name: 'Confirm roles' }), null)
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    await screen.findByText('Drawing profile applied.', {}, { timeout: 15000 })
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    const reused = await rpc(next, 'load', next)
    assert.equal(reused.application_record.version, 2)
    assert.equal(reused.application_record.profile_id, pin.application_record.profile_id)
    cleanup()

    mount(first)
    await screen.findByRole('button', { name: 'Use copy 1' }, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    await screen.findByText('Applied version: 1', {}, { timeout: 15000 })
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    pin = await rpc(first, 'load', first)
    assert.equal(pin.application_record.version, 1)
    assert.equal(calls.filter(row => row.op === 'confirm').length, 1)
    console.log('PASS: actual JSX callbacks through Python JSON adapter, one confirmation, copy reuse, editor version2 and old pinned reopen1')
  } finally {
    cleanup(); dom.window.close()
    // Workers normally all exit before callback resolution; retire any failed
    // operation before removing only this harness-owned disposable database.
    await Promise.all([...children].map(child => new Promise(resolve => { child.once('close', resolve); child.kill() })))
    if (fs.existsSync(database)) fs.unlinkSync(database)
    fs.rmdirSync(root)
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
