// @vitest-environment jsdom
// Fault injection against the real worker JS, without a compiled engine.
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import useEngineSession from '../cadedit/engineSession.js'

function createEditorWorker() {
  return new Worker(
    new URL('../../../vendor/acadrust-worker/worker-browser.mjs', import.meta.url),
    { type: 'module' },
  )
}
function captureWorkerPath() {
  class CapturingWorker {
    constructor(url) {
      const u = url instanceof URL ? url : new URL(String(url))
      CapturingWorker.captured = u.protocol === 'file:'
        ? fileURLToPath(u)
        : decodeURIComponent(u.pathname.replace(/^\/@fs\//, '/').replace(/^\/(?=[A-Za-z]:)/, ''))
    }
  }
  const previous = globalThis.Worker
  globalThis.Worker = CapturingWorker
  try { createEditorWorker() }
  finally {
    if (previous === undefined) delete globalThis.Worker
    else globalThis.Worker = previous
  }
  return CapturingWorker.captured
}
const WORKER_PATH = captureWorkerPath()
const common = { layer: '0', editable: true, aci: 256, linetype: 'ByLayer', lineweight: -1 }
const D = [
  { ...common, id: '16', type: 'LINE', vertices: [[0, 0, 0], [10, 0, 0]] },
  { ...common, id: '17', type: 'LINE', vertices: [[0, 10, 0], [10, 10, 0]] },
  { ...common, id: '18', type: 'LINE', vertices: [[20, 0, 0], [20, 10, 0]] },
  { ...common, id: '19', type: 'CIRCLE', vertices: [[30, 10, 0]], radius: 5 },
]
D.linetypes = ['ByLayer', 'ByBlock', 'Continuous', 'DASHED']
const DXF = '0\nSECTION\n2\nENTITIES\n0\nLINE\n5\n10\n8\n0\n10\n0\n20\n0\n11\n10\n21\n0\n0\nLINE\n5\n11\n8\n0\n10\n0\n20\n10\n11\n10\n21\n10\n0\nLINE\n5\n12\n8\n0\n10\n20\n20\n0\n11\n20\n21\n10\n0\nCIRCLE\n5\n13\n8\n0\n10\n30\n20\n10\n40\n5\n0\nENDSEC\n0\nEOF\n'
const S = ['16', '19']
const batch = (op = 'move', ids = S) => ({
  type: 'applyEdit', op: 'batch',
  payload: { verb: op, steps: ids.map((entityId) => ({ op, payload: { entityId, dx: 2, dy: 3 } })) },
})
// This subprocess imports the worker through the fence-approved captured URL.
// The mock parses the fixture's DXF handles and uses JSON only for snapshots.
const SCRIPT = String.raw`
import { pathToFileURL } from 'node:url'
const [workerPath, optionsJson, dxf] = process.argv.slice(1)
const options = JSON.parse(optionsJson)
const { handleMessage } = await import(pathToFileURL(workerPath).href)
let writes = 0, parses = 0, calls = 0, held = null
const encoder = new TextEncoder(), decoder = new TextDecoder()
function parseFixture(text) {
  const lines = text.trim().split(/\r?\n/)
  const records = []
  for (let i = 0; i < lines.length; i += 2) {
    if (lines[i] !== '0' || !['LINE', 'CIRCLE'].includes(lines[i + 1])) continue
    const type = lines[i + 1], fields = {}
    for (i += 2; i < lines.length && lines[i] !== '0'; i += 2) fields[lines[i]] = lines[i + 1]
    i -= 2
    records.push({ handle: BigInt('0x' + fields['5']).toString(), type, layer: fields['8'],
      editable: true, vertices: type === 'LINE'
        ? [[Number(fields['10']), Number(fields['20']), 0], [Number(fields['11']), Number(fields['21']), 0]]
        : [[Number(fields['10']), Number(fields['20']), 0]],
      ...(type === 'CIRCLE' ? { radius: Number(fields['40']) } : {}) })
  }
  return records
}
const engine = {
  parseDxf(bytes) {
    parses++
    if (options.fault === 'parse' && parses === 2) throw new Error('injected_parse')
    if (options.fault === 'restore' && parses === 2) throw new Error('injected_restore')
    const text = decoder.decode(bytes)
    const rows = text.startsWith('[') ? JSON.parse(text) : parseFixture(text)
    const final = parses === 2
    const doc = {
      rows,
      editableEntities() {
        if (final && options.fault === 'projection') throw new Error('injected_projection')
        const projected = rows.map((row, index) => ({ ...row, index }))
        return final && options.fault === 'created' ? projected.filter(row => row.handle !== '20') : projected
      },
      translateEntity(index, dx, dy) {
        calls++
        rows[index].vertices = rows[index].vertices.map(([x, y, z]) => [x + dx, y + dy, z])
      },
      deleteEntity(index) { calls++; rows.splice(index, 1) },
      copyEntity(index, dx, dy) {
        calls++
        const handle = (rows.reduce((max, row) => BigInt(row.handle) > max ? BigInt(row.handle) : max, 0n) + 1n).toString()
        rows.push({ ...rows[index], handle, vertices: rows[index].vertices.map(([x, y, z]) => [x + dx, y + dy, z]) })
        return handle
      },
    }
    held = doc
    return doc
  },
  writeDxf(doc) {
    writes++
    if (options.fault === 'snapshot' && writes === 1) throw new Error('injected_snapshot')
    if (options.fault === 'write' && writes === 2) throw new Error('injected_write')
    return encoder.encode(JSON.stringify(doc.rows))
  },
}
let loaded = null
if (!options.noLoad) loaded = await handleMessage({ type: 'loadDocument', documentId: 'one.dxf', bytes: encoder.encode(dxf) }, engine)
const before = held ? JSON.stringify(held.rows) : null
const reply = await handleMessage(options.message, engine)
const state = held ? JSON.stringify(held.rows) : null
const counters = { writes, parses, calls }
let probe = null
if (options.fault === 'restore') probe = await handleMessage({ type: 'applyEdit', op: 'move', payload: { entityId: '16', dx: 0, dy: 0 } }, engine)
process.stdout.write(JSON.stringify({ reply, loaded, before, state, ...counters, probe }))
`
function raw(message = batch(), fault = null, noLoad = false) {
  return JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', SCRIPT, WORKER_PATH, JSON.stringify({ message, fault, noLoad }), DXF], {
    encoding: 'utf8', timeout: 30_000, maxBuffer: 4 * 1024 * 1024,
  }))
}
class ScriptedWorker {
  posted = []
  listeners = new Set()
  addEventListener(type, fn) { if (type === 'message') this.listeners.add(fn) }
  removeEventListener(type, fn) { if (type === 'message') this.listeners.delete(fn) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.listeners.clear() }
  emit(message) { act(() => this.listeners.forEach((fn) => fn({ data: message }))) }
}
function session() {
  const worker = new ScriptedWorker()
  const { result } = renderHook(() => useEngineSession({ createWorker: () => worker }))
  const b0 = new TextEncoder().encode(DXF)
  const open = () => {
    act(() => result.current.actions.openBytes(b0, 'one.dxf'))
    worker.emit({ type: 'documentLoaded', documentId: 'one.dxf', entities: D, entityCount: D.length, unsupported: [] })
    act(() => result.current.actions.selectReplace(S))
    worker.posted.length = 0
  }
  open()
  return { worker, result, open }
}
function assertRefusal(out, reason, op = 'move') {
  expect(out.reply).toEqual({ type: 'editApplied', op: 'batch', ok: false, reason })
  expect(out.state).toBe(out.before)
  const h = session()
  const before = h.result.current
  act(() => h.result.current.actions.applyEdit(op, { dx: '2', dy: '3' }))
  expect(h.worker.posted).toHaveLength(1)
  h.worker.emit(out.reply)
  expect(h.result.current.status).toBe('Edit refused (' + op + '): ' + reason)
  expect(h.result.current.entities).toBe(before.entities)
  expect(h.result.current.selectedIds).toEqual(S)
  expect(h.result.current.savedBytes).toBe(before.savedBytes)
  expect(h.result.current.undoDepth).toBe(0)
  expect(h.result.current.redoDepth).toBe(0)
}
afterEach(cleanup)
it('MS19 failed-step', () => {
  const out = raw(batch('move', ['16', '999']))
  assertRefusal(out, 'step_1_move:bad_entity_id')
  expect(out.calls).toBe(1)
  expect(out.parses).toBe(2)
})
it('MS20 envelope-refusals', () => {
  for (const payload of [{}, { steps: [] }]) assertRefusal(raw({ type: 'applyEdit', op: 'batch', payload }), 'batch_empty')
  assertRefusal(raw(batch('move', Array(257).fill('16'))), 'batch_too_many_steps')
  assertRefusal(raw({ type: 'applyEdit', op: 'batch', payload: { steps: [{ op: 'batch' }] } }), 'step_0_batch_nested')
  assertRefusal(raw({ type: 'applyEdit', op: 'batch', payload: { steps: [{ op: 'hasOwnProperty', payload: { entityId: '16' } }] } }), 'step_0_hasOwnProperty:unknown_op:hasOwnProperty')
  expect(raw(batch(), null, true).reply).toEqual({ type: 'editApplied', op: 'batch', ok: false, reason: 'no_document_loaded' })
  // Six synchronous worker subprocesses: about 5.2 s alone on this host, past vitest's 5 s default.
}, 30_000)
it('MS21 snapshot-failure', () => {
  const out = raw(batch(), 'snapshot')
  assertRefusal(out, 'batch_snapshot_failed:injected_snapshot')
  expect(out.calls).toBe(0)
  expect(out.writes).toBe(1)
  expect(out.parses).toBe(1)
})
it('MS22 final-write-failure', () => {
  const out = raw(batch(), 'write')
  assertRefusal(out, 'batch_write_failed:injected_write')
  expect(out.calls).toBe(2)
  expect(out.writes).toBe(2)
  expect(out.parses).toBe(2)
})
it('MS23 reparse-failure', () => {
  const out = raw(batch(), 'parse')
  assertRefusal(out, 'batch_parse_failed:injected_parse')
  expect(out.calls).toBe(2)
  expect(out.parses).toBe(3)
})
it('MS24 projection-failure', () => {
  const out = raw(batch(), 'projection')
  assertRefusal(out, 'batch_projection_failed:injected_projection')
  expect(out.parses).toBe(3)
})
it('MS25 restore-failure', () => {
  const out = raw(batch('move', ['16', '999']), 'restore')
  expect(out.reply).toEqual({ type: 'error', message: 'batch_restore_failed:injected_restore' })
  expect(out.probe).toEqual({ type: 'editApplied', op: 'move', ok: false, reason: 'no_document_loaded' })
  const h = session()
  act(() => h.result.current.actions.applyEdit('move', { dx: '2', dy: '3' }))
  h.worker.emit(out.reply)
  expect(h.result.current.engineParsed).toBe(false)
  expect(h.result.current.selectedIds).toEqual([])
  expect(h.result.current.undoDepth).toBe(0)
  expect(h.result.current.status).toBe('Engine refused: batch_restore_failed:injected_restore')
})
it('MS26 created-id-loss', () => {
  const out = raw(batch('copy'), 'created')
  assertRefusal(out, 'batch_projection_failed:created_entity_lost', 'copy')
  expect(out.calls).toBe(2)
  expect(out.parses).toBe(3)
})
it('MS27 inflight-lifetime', () => {
  const h = session()
  act(() => {
    h.result.current.actions.applyEdit('move', { dx: '2', dy: '3' })
    h.result.current.actions.applyEdit('move', { dx: '2', dy: '3' })
  })
  expect(h.worker.posted).toHaveLength(1)
  h.worker.emit({ type: 'editApplied', op: 'batch', ok: false, reason: 'injected' })
  act(() => h.result.current.actions.applyEdit('move', { dx: '2', dy: '3' }))
  expect(h.worker.posted).toHaveLength(2)
  for (const clear of ['reset', 'document', 'error']) {
    h.open()
    act(() => h.result.current.actions.applyEdit('copy', { dx: '2', dy: '3' }))
    if (clear === 'reset') act(() => h.result.current.actions.reset())
    if (clear === 'error') h.worker.emit({ type: 'error', message: 'injected' })
    // The error branch must clear the batch context itself: reopening here
    // would clear it too and hide a missing cleanup, so only reset and the
    // new document reopen before the unrelated batch.
    if (clear !== 'error') h.open()
    // An unrelated batch must use the ordinary single-created-entity policy.
    h.worker.emit({ type: 'editApplied', op: 'batch', ok: true, createdId: '21', createdIds: ['20', '21'],
      entities: [...D, { ...D[0], id: '20' }, { ...D[3], id: '21' }], entityCount: 6,
      bytes: new Uint8Array([1, 2]), byteLength: 2 })
    expect(h.result.current.selectedIds).toEqual(['21'])
    expect(h.result.current.selectedId).toBe('21')
  }
})
it('selection batch success serializes twice and reparses once, resolving shifted delete indices', () => {
  const moved = raw()
  expect(moved.reply.ok).toBe(true)
  expect(moved.writes).toBe(2)
  expect(moved.parses).toBe(2)
  expect(moved.reply.entities.find(e => e.id === '16').vertices).toEqual([[2, 3, 0], [12, 3, 0]])
  expect(moved.reply.entities.find(e => e.id === '19').vertices).toEqual([[32, 13, 0]])
  const deleted = raw(batch('delete', ['16', '18']))
  expect(deleted.reply.entities.map(e => e.id)).toEqual(['17', '19'])
  expect(deleted.reply.entities.find(e => e.id === '17').vertices).toEqual(D[1].vertices)
  expect(deleted.reply.entities.find(e => e.id === '19').vertices).toEqual(D[3].vertices)
})
