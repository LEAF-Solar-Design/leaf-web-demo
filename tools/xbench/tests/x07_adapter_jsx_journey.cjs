// Actual JSX -> local JSON stdin/stdout -> Python WorkflowAdapter -> SQLite.
// No browser/network and no mocked confirmation/apply persistence callbacks.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os')
const Module = require('node:module'), { spawn } = require('node:child_process')
const { StringDecoder } = require('node:string_decoder')
const { TextDecoder } = require('node:util')
const assert = require('node:assert/strict')
const at = process.argv.indexOf('--deps'), deps = path.resolve(at < 0 ? 'web/node_modules' : process.argv[at + 1])
const dependency = name => require(path.join(deps, name))
const { JSDOM } = dependency('jsdom')
const dom = new JSDOM('<html><body></body></html>', { url: 'http://localhost' })
global.window = dom.window; global.document = dom.window.document
Object.defineProperty(global, 'navigator', { configurable: true, value: dom.window.navigator })
global.HTMLElement = dom.window.HTMLElement; global.MutationObserver = dom.window.MutationObserver
global.fetch = dom.window.fetch = () => { throw new Error('Offline JSON journey forbids network') }
const React = dependency('react'), { render, screen, fireEvent, cleanup, waitFor, within } = dependency('@testing-library/react')
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
from contextlib import nullcontext
from unittest.mock import patch
sys.path.insert(0,str(Path.cwd()/'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter
class DuplicateKeyError(ValueError): pass

def reject_constant(value):
    raise ValueError('invalid JSON constant')

def check_depth(raw):
    depth=0;quoted=False;escaped=False
    for char in raw:
        if quoted:
            if escaped: escaped=False
            elif char=='\\\\': escaped=True
            elif char=='"': quoted=False
        elif char=='"': quoted=True
        elif char in '{[':
            depth+=1
            if depth>32: raise OverflowError('JSON nesting limit')
        elif char in '}]': depth-=1

def unique_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise DuplicateKeyError('ambiguous duplicate JSON key')
        result[key]=value
    return result
try:
    raw=sys.stdin.buffer.read().decode('utf-8',errors='strict')
    check_depth(raw)
    request=json.loads(raw,object_pairs_hook=unique_pairs,parse_constant=reject_constant)
except UnicodeDecodeError:
    print(json.dumps({'ok':False,'error':{'message':'Invalid UTF-8 request.','code':'BRIDGE_UTF8'}}));sys.exit(0)
except DuplicateKeyError:
    print(json.dumps({'ok':False,'error':{'message':'Ambiguous duplicate or invalid JSON request.','code':'BRIDGE_DUPLICATE_JSON_KEY'}}));sys.exit(0)
except (OverflowError,RecursionError):
    print(json.dumps({'ok':False,'error':{'message':'JSON nesting limit exceeded.','code':'BRIDGE_JSON_DEPTH'}}));sys.exit(0)
except ValueError:
    print(json.dumps({'ok':False,'error':{'message':'Invalid JSON request.','code':'BRIDGE_MALFORMED_JSON'}}));sys.exit(0)
protocol=request.get('protocol_fault')
if protocol=='json_grammar': print(request['fixture']['reply_json']);sys.exit(0)
if protocol=='invalid_utf8':
    fixture=request['fixture']
    invalid=bytes(fixture['utf8_bytes'])
    tail=b'' if fixture.get('utf8_truncated') else b'"}}'
    sys.stdout.buffer.write(b'{"ok":true,"result":{"label":"'+invalid+tail)
    sys.stdout.buffer.flush();sys.exit(0)
if protocol and protocol.startswith('duplicate_reply_'):
    key={'duplicate_reply_customer':'customer_key','duplicate_reply_version':'version','duplicate_reply_role':'block'}[protocol]
    body='{"'+key+'":1,"'+key+'":2}'
    if key=='block': body='{"roles":{"module":'+body+'}}'
    print('{"ok":true,"result":'+body+'}');sys.exit(0)
if protocol=='eof': sys.exit(0)
if protocol=='malformed': print('{not JSON'); sys.exit(0)
if protocol=='nonzero': sys.stderr.write('injected protocol exit'); sys.exit(23)
if protocol=='hung':
    import time
    time.sleep(60)
if protocol=='stdout_large': sys.stdout.write('x'*(2*1024*1024));sys.stdout.flush();sys.exit(0)
if protocol=='stderr_large': sys.stderr.write('x'*(128*1024));sys.stderr.flush();sys.exit(23)
try:
    store=CustomerProfileStore(request['database'])
    adapter=WorkflowAdapter(ProfileWorkflow(store),context=request['bound_context'],intake=request['intake'],copies=request['copies'])
    op=request['op']; value=request.get('value'); context=request['context']
    target={'pin':'apply_to_project','save':'save_version'}.get(request.get('fault'))
    with patch.object(store,target,side_effect=OSError('injected offline '+str(request.get('fault'))+' failure')) if target else nullcontext():
        if op=='load': result=adapter.load_workflow(context)
        elif op=='confirm': result=adapter.confirm_roles(context,value)
        elif op=='apply': result=adapter.apply_profile(context,value)
        elif op=='editor_load': result=adapter.load_profile(value)
        elif op=='editor_save': result=adapter.save_profile(value['customer'],value['roles'],expected_version=value['expected_version'])
        elif op=='seed_max':
            with store._connection() as db:
                db.execute('INSERT INTO profile_versions SELECT customer_key,profile_id,?,roles_json,created_at FROM profile_versions WHERE customer_key=? AND version=1',(2**53-1,context['customer_key']))
            result=adapter.load_profile(context['customer_key'])
        else: raise ValueError('unknown offline operation')
    fixture=request.get('fixture',{})
    if op=='editor_load' and 'receipt_version' in fixture:
        result['version']=fixture['receipt_version'] # malformed-producer fixture, no write
    print(json.dumps({'ok':True,'result':result},ensure_ascii=False))
except Exception as error:
    print(json.dumps({'ok':False,'error':{'type':type(error).__name__,'message':str(error),'code':getattr(error,'code',None)}}))
`
const intake = { inserts: [0, 1, 2, 10, 11, 12].map(x => ({ name: 'Panel', layer: 'MODULES', x, y: 0 })),
  polylines: [{ layer: 'ROOF' }, { layer: 'OBSTRUCTION' }] }
const copies = [0, 10].map(x => ({ block: 'Panel', layer: 'MODULES', design_window: [x, 0, x + 2, 0], oracle_count: 3 }))
const ctx = (project, drawing = project + ':v1') => ({ customer_key: 'synthetic-a', project_key: project, drawing_key: drawing })
const calls = [], children = new Set()
const protocolStats = []
// JSON.parse validates grammar first. This second bounded walk retains member
// names that JSON.parse would otherwise overwrite, including escaped aliases.
function strictJSON(text) {
  let depth = 0, quoted = false, escaped = false
  for (const char of text) {
    if (quoted) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === '"') quoted = false
    } else if (char === '"') quoted = true
    else if (char === '{' || char === '[') {
      if (++depth > 32) { const error = new Error('JSON nesting limit exceeded.'); error.code = 'BRIDGE_JSON_DEPTH'; throw error }
    } else if (char === '}' || char === ']') depth--
  }
  const value = JSON.parse(text); let index = 0
  const whitespace = () => { while (/\s/.test(text[index] || '') && index < text.length) index++ }
  const string = () => {
    const start = index++
    while (index < text.length) { const char = text[index++]; if (char === '\\') index++; else if (char === '"') break }
    return JSON.parse(text.slice(start, index))
  }
  const walk = () => {
    whitespace()
    if (text[index] === '{') {
      index++; whitespace(); const names = new Set()
      while (text[index] !== '}') {
        const key = string()
        if (names.has(key)) { const error = new Error('Ambiguous duplicate JSON key.'); error.code = 'BRIDGE_DUPLICATE_JSON_KEY'; throw error }
        names.add(key); whitespace(); index++; walk(); whitespace()
        if (text[index] !== ',') break
        index++; whitespace()
      }
      index++
    } else if (text[index] === '[') {
      index++; whitespace()
      while (text[index] !== ']') { walk(); whitespace(); if (text[index] !== ',') break; index++ }
      index++
    } else if (text[index] === '"') string()
    else { while (index < text.length && !/[\s,}\]]/.test(text[index])) index++ }
  }
  walk(); return value
}
function rpc(bound, op, context, value, signal, fault = null, protocolFault = null, fixture = {}) {
  calls.push({ op, context, value })
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new Error('retired')); return }
    const child = spawn('python', ['-X', 'utf8', '-c', python], { cwd: path.resolve(__dirname, '../../..'), stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
    children.add(child)
    let stdout = '', stderr = '', finished = false, stdoutBytes = 0, stderrBytes = 0, terminalError = null
    const raw = []
    const stop = (code, message) => { if (!terminalError) { terminalError = new Error(message); terminalError.code = code }; child.kill() }
    const timer = setTimeout(() => stop('BRIDGE_TIMEOUT', 'Python JSON worker timed out.'), fixture.timeout_ms || 15000)
    const abort = () => child.kill()
    signal?.addEventListener('abort', abort, { once: true })
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', chunk => {
      stdoutBytes += chunk.length
      if (stdoutBytes > 1024*1024) stop('BRIDGE_STDOUT_LIMIT', 'Python JSON worker output limit exceeded.')
      else raw.push(chunk)
    })
    child.stderr.on('data', chunk => {
      stderrBytes += Buffer.byteLength(chunk)
      if (stderrBytes > 64*1024) stop('BRIDGE_STDERR_LIMIT', 'Python JSON worker diagnostic limit exceeded.')
      else stderr += chunk
    })
    const settle = (error, result) => {
      if (finished) return
      finished = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); children.delete(child)
      error ? reject(error) : resolve(result)
    }
    child.on('error', error => settle(error))
    child.stdin.on('error', error => settle(error))
    child.on('close', code => {
      if (terminalError) { protocolStats.push({ stdout_bytes: stdoutBytes, stderr_bytes: stderrBytes, stored_stdout_bytes: raw.reduce((n,b)=>n+b.length,0), stored_stderr_bytes: Buffer.byteLength(stderr), receipts: 0 }); settle(terminalError); return }
      // Split raw bytes deterministically in the receiver fixture. OS pipe
      // coalescing cannot turn a split-codepoint test into a whole-string test.
      let bytes = Buffer.concat(raw)
      if (fixture.truncate_bytes) bytes = bytes.subarray(0, Math.max(0, bytes.length-fixture.truncate_bytes))
      if (fixture.truncate_json_bytes) {
        let end = bytes.length
        while (end && [9,10,13,32].includes(bytes[end-1])) end--
        bytes = bytes.subarray(0, Math.max(0, end-fixture.truncate_json_bytes))
      }
      try { new TextDecoder('utf-8', { fatal: true }).decode(bytes) }
      catch {
        const error = new Error('Python JSON worker returned invalid UTF-8.'); error.code = 'BRIDGE_UTF8'
        protocolStats.push({ bytes: bytes.length, parts: 0, receipts: 0 }); settle(error); return
      }
      const decoder = new StringDecoder('utf8'), pattern = fixture.chunk_pattern || [Math.max(1, bytes.length)]
      let offset = 0, parts = 0
      while (offset < bytes.length) {
        const length = pattern[parts % pattern.length]
        if (!Number.isSafeInteger(length) || length < 1) { settle(new Error('Invalid fixture chunk pattern.')); return }
        stdout += decoder.write(bytes.subarray(offset, offset+length)); offset += length; parts++
      }
      stdout += decoder.end(); protocolStats.push({ bytes: bytes.length, parts, receipts: 0 })
      if (code !== 0) { const error = new Error('Python JSON worker failed: ' + stderr.slice(0, 500)); error.code = 'BRIDGE_EXIT'; settle(error); return }
      if (!stdout.trim()) { const error = new Error('Python JSON worker returned no JSON.'); error.code = 'BRIDGE_EOF'; settle(error); return }
      try {
        const response = strictJSON(stdout)
        if (!response.ok) { const error = new Error(response.error.message); error.code = response.error.code; settle(error) }
        else { protocolStats[protocolStats.length-1].receipts++; settle(null, response.result) }
      } catch (cause) { const error = new Error(cause.code === 'BRIDGE_DUPLICATE_JSON_KEY' ? cause.message : 'Python JSON worker returned malformed JSON.'); error.code = cause.code || 'BRIDGE_MALFORMED_JSON'; settle(error) }
    })
    let request = JSON.stringify({ database, bound_context: bound, context, op, value, intake, copies, fault, protocol_fault: protocolFault, fixture })
    if (fixture.duplicate_request_key) {
      const token = JSON.stringify(fixture.duplicate_request_key) + ':'
      request = request.replace(token, token + 'null,' + token)
    }
    if (fixture.request_json_value !== undefined) {
      request = request.slice(0, -1) + ',"grammar":' + fixture.request_json_value + '}'
    }
    if (fixture.trailing_json) request += ' {}'
    let requestBytes = Buffer.from(request, 'utf8')
    if (fixture.invalid_request_utf8) {
      const invalid = Buffer.from(fixture.utf8_bytes)
      if (fixture.utf8_truncated) requestBytes = Buffer.concat([requestBytes, invalid])
      else {
        const start = requestBytes.indexOf(Buffer.from('Panel'))
        if (start < 0) { stop('BRIDGE_FIXTURE', 'Missing fixture label.'); return }
        requestBytes = Buffer.concat([requestBytes.subarray(0, start), invalid, requestBytes.subarray(start + 5)])
      }
    }
    child.stdin.end(requestBytes)
  })
}
function mount(context, overrides = {}) {
  return render(React.createElement(Workflow, { customerKey: context.customer_key,
    projectKey: context.project_key, drawingKey: context.drawing_key,
    loadWorkflow: (scope, opts) => rpc(context, 'load', scope, null, opts.signal),
    confirmRoles: (scope, value, opts) => rpc(context, 'confirm', scope, value, opts.signal),
    applyProfile: (scope, value, opts) => rpc(context, 'apply', scope, value, opts.signal),
    loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
    saveProfile: (customer, roles, opts) => rpc(context, 'editor_save', context,
      { customer, roles, expected_version: opts.expected_version }, opts.signal), ...overrides }))
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
  } finally { await dispose() }
}
async function dispose() {
    cleanup(); dom.window.close()
    // Workers normally all exit before callback resolution; retire any failed
    // operation before removing only this harness-owned disposable database.
    await Promise.all([...children].map(child => new Promise(resolve => { child.once('close', resolve); child.kill() })))
    if (fs.existsSync(database)) fs.unlinkSync(database)
    fs.rmdirSync(root)
}
module.exports = { strictJSON, rpc, ctx, copies, intake, mount, screen, fireEvent, waitFor, cleanup, dispose, component, React, render, within, protocolStats,
  resources: () => ({ child_count: children.size, database, root }) }
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1 })
