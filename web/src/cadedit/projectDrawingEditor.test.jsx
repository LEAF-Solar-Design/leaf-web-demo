import { webcrypto } from 'node:crypto'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createProjectDrawingEditor } from './projectDrawingEditor.js'
import useEngineSession from './engineSession.js'

const ORG = '10000000-0000-4000-8000-000000000001'
const PROJECT = '10000000-0000-4000-8000-000000000002'
const DRAWING = '10000000-0000-4000-8000-000000000003'
const PARENT = '10000000-0000-4000-8000-000000000004'
const NEXT = '10000000-0000-4000-8000-000000000005'
const REQUEST = '10000000-0000-4000-8000-000000000006'
const OTHER = '20000000-0000-4000-8000-000000000001'
const bytes = new TextEncoder().encode('0\nEOF\n')
const scope = () => ({ organization_id: ORG, project_id: PROJECT, drawing_id: DRAWING, input_version_id: PARENT })
const digest = async (value = bytes) => Array.from(new Uint8Array(await webcrypto.subtle.digest('SHA-256', value)), n => n.toString(16).padStart(2, '0')).join('')
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const version = (overrides = {}) => ({ version_id: NEXT, drawing_id: DRAWING, project_id: PROJECT, org_id: ORG, seq: 2, ...overrides })
async function receipt(overrides = {}) {
  return { drawing_version: version(), organization_id: ORG, project_id: PROJECT, drawing_id: DRAWING,
    parent_version_id: PARENT, request_id: REQUEST, source_sha256: await digest(), intake_sha256: 'a'.repeat(64), ...overrides }
}
function harness(options = {}) {
  let current = scope()
  const transport = { read: vi.fn(async () => ({ bytes, source_sha256: await digest() })),
    save: vi.fn(async () => receipt()), restore: vi.fn(async () => receipt()), ...options.transport }
  const acquireCheckout = vi.fn(async () => ({ capability: 'test-private-capability' }))
  const releaseCheckout = vi.fn(async () => {})
  const importFile = vi.fn(async () => ({ drawing_version: version({ seq: 1 }) }))
  const editor = createProjectDrawingEditor({ getScope: () => current, importFile,
    acquireCheckout, releaseCheckout, newRequestId: () => REQUEST, ...options, transport })
  return { editor, transport, acquireCheckout, releaseCheckout, importFile, setScope: value => { current = value } }
}
beforeEach(() => vi.stubGlobal('crypto', webcrypto))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('project editor custody', () => {
  it('imports into the selected empty project before returning canonical identity', async () => {
    const h = harness()
    h.setScope({ ...scope(), drawing_id: null, input_version_id: null })
    const file = { name: 'roof.dxf', size: bytes.length }
    const result = await h.editor.getSaveTarget().openFile(file)
    expect(h.importFile).toHaveBeenCalledWith(file, { ...scope(), drawing_id: null, input_version_id: null })
    expect(result.drawing_version.version_id).toBe(NEXT)
    expect(result.projectOperation).toBe('import')
    expect(h.transport.save).not.toHaveBeenCalled()
  })

  it('rejects a foreign imported project identity', async () => {
    const h = harness({ importFile: async () => ({ drawing_version: version({ project_id: OTHER }) }) })
    await expect(h.editor.openFile({ size: 5 })).rejects.toMatchObject({ status: 409 })
  })

  it('drops an import that completes after project selection changes', async () => {
    const late = deferred()
    const h = harness({ importFile: () => late.promise })
    const opening = h.editor.openFile({ size: 5 })
    h.setScope({ ...scope(), project_id: OTHER })
    h.editor.getSaveTarget()
    late.resolve({ drawing_version: version() })
    await expect(opening).rejects.toMatchObject({ code: 'project_editor_scope_changed' })
  })

  it('reads the exact canonical version and checks immutable source bytes', async () => {
    const h = harness()
    const result = await h.editor.readVersion(PARENT)
    expect(h.transport.read).toHaveBeenCalledWith(scope())
    expect(Array.from(result.bytes)).toEqual(Array.from(bytes))
    expect(result.versionId).toBe(PARENT)
  })

  it('refuses mismatching downloaded bytes', async () => {
    const h = harness({ transport: { read: async () => ({ bytes, source_sha256: 'b'.repeat(64) }) } })
    await expect(h.editor.readVersion(PARENT)).rejects.toMatchObject({ status: 409 })
  })

  it('drops a late immutable read after scope changes', async () => {
    const late = deferred()
    const h = harness({ transport: { read: () => late.promise } })
    const reading = h.editor.readVersion(PARENT)
    h.setScope({ ...scope(), drawing_id: OTHER })
    h.editor.getSaveTarget()
    late.resolve({ bytes, source_sha256: await digest() })
    await expect(reading).rejects.toMatchObject({ status: 409 })
  })

  it('does not seat an older read that completes after a newer read in the same project', async () => {
    const late = deferred()
    const h = harness()
    h.transport.read.mockImplementationOnce(() => late.promise)
    const first = h.editor.readVersion(PARENT)
    await h.editor.readVersion(NEXT)
    late.resolve({ bytes, source_sha256: await digest() })
    await expect(first).rejects.toMatchObject({ status: 409 })
  })

  it('publishes through a scoped acquired checkout and releases only its own checkout', async () => {
    const h = harness()
    const result = await h.editor.getSaveTarget().save(bytes, PARENT, await digest())
    expect(h.acquireCheckout).toHaveBeenCalledWith(scope())
    expect(h.transport.save.mock.calls[0][0]).toEqual(scope())
    expect(Array.from(h.transport.save.mock.calls[0][1])).toEqual(Array.from(bytes))
    expect(h.transport.save.mock.calls[0][2]).toEqual({ requestId: REQUEST, capability: 'test-private-capability' })
    expect(h.releaseCheckout).toHaveBeenCalledWith(scope(), 'test-private-capability')
    expect(result.drawing_version.version_id).toBe(NEXT)
    expect(result.new_version).toBeUndefined()
  })

  it('preserves a caller-owned checkout', async () => {
    const h = harness({ getCapability: () => 'already-held-capability' })
    await h.editor.save(bytes, PARENT, await digest())
    expect(h.acquireCheckout).not.toHaveBeenCalled()
    expect(h.releaseCheckout).not.toHaveBeenCalled()
  })

  it('refuses stale parent and changed byte digest before checkout or save', async () => {
    const h = harness()
    await expect(h.editor.save(bytes, OTHER, await digest())).rejects.toMatchObject({ status: 409 })
    await expect(h.editor.save(bytes, PARENT, 'b'.repeat(64))).rejects.toMatchObject({ status: 409 })
    expect(h.acquireCheckout).not.toHaveBeenCalled()
    expect(h.transport.save).not.toHaveBeenCalled()
  })

  it.each(['organization_id', 'project_id', 'drawing_id', 'parent_version_id', 'request_id'])('refuses wrong publication %s', async (key) => {
    const h = harness({ transport: { save: async () => receipt({ [key]: OTHER }) } })
    await expect(h.editor.save(bytes, PARENT, await digest())).rejects.toMatchObject({ outcomeUnknown: true })
    expect(h.releaseCheckout).not.toHaveBeenCalled()
  })

  it('retries unknown publication with the same request, original bytes and held capability', async () => {
    const h = harness()
    h.transport.save.mockRejectedValueOnce(Object.assign(new Error('network'), { outcomeUnknown: true }))
    await expect(h.editor.save(bytes, PARENT, await digest())).rejects.toThrow('network')
    expect(h.releaseCheckout).not.toHaveBeenCalled()
    await h.editor.save(new Uint8Array(bytes), PARENT, await digest())
    expect(h.acquireCheckout).toHaveBeenCalledTimes(1)
    expect(h.transport.save.mock.calls[0][2]).toEqual(h.transport.save.mock.calls[1][2])
    expect(h.transport.save.mock.calls[0][1]).toBe(h.transport.save.mock.calls[1][1])
    expect(h.releaseCheckout).toHaveBeenCalledTimes(1)
  })

  it('refuses different edits while the previous publication outcome is unknown', async () => {
    const h = harness({ transport: { save: async () => { throw new Error('network') } } })
    await expect(h.editor.save(bytes, PARENT, await digest())).rejects.toThrow('network')
    const changed = new Uint8Array([5])
    await expect(h.editor.save(changed, PARENT, await digest(changed))).rejects.toMatchObject({ status: 409 })
  })

  it('refuses stale save adoption after scope changes while releasing settled custody', async () => {
    const late = deferred()
    const h = harness({ transport: { save: () => late.promise } })
    const saving = h.editor.save(bytes, PARENT, await digest())
    await vi.waitFor(() => expect(h.acquireCheckout).toHaveBeenCalledTimes(1))
    h.setScope({ ...scope(), project_id: OTHER })
    h.editor.getSaveTarget()
    late.resolve(await receipt())
    await expect(saving).rejects.toMatchObject({ status: 409 })
    expect(h.releaseCheckout).toHaveBeenCalledWith(scope(), 'test-private-capability')
  })

  it('restores a same-artifact predecessor through the project publication transport', async () => {
    const h = harness()
    await h.editor.restoreVersion(OTHER)
    expect(h.transport.restore).toHaveBeenCalledWith(scope(), OTHER, { requestId: REQUEST, capability: 'test-private-capability' })
    expect(h.releaseCheckout).toHaveBeenCalledTimes(1)
  })
})

class WorkerTransport {
  constructor() { this.listeners = { message: [], error: [], messageerror: [] }; this.posted = []; this.terminated = false }
  addEventListener(type, fn) { this.listeners[type]?.push(fn) }
  removeEventListener() {}
  postMessage(value) { this.posted.push(value) }
  terminate() { this.terminated = true }
}

describe('engine project adapter', () => {
  it('routes project Open DXF to adoption without opening a local worker', async () => {
    const imported = { drawing_version: version(), projectOperation: 'import' }
    const createWorker = vi.fn()
    const onSaved = vi.fn()
    const target = { kind: 'project', scopeKey: 'project1', openFile: vi.fn(async () => imported) }
    const { result } = renderHook(() => useEngineSession({ createWorker, saveTarget: target, onSaved }))
    await act(async () => { await result.current.actions.open({ name: 'roof.dxf', size: 5 }) })
    expect(target.openFile).toHaveBeenCalledTimes(1)
    expect(createWorker).not.toHaveBeenCalled()
    expect(onSaved).toHaveBeenCalledWith(imported)
  })

  it('does not emit import completion onto a switched project target', async () => {
    const late = deferred()
    const onSaved = vi.fn()
    const createWorker = vi.fn()
    const first = { kind: 'project', scopeKey: 'first', openFile: () => late.promise }
    const { result, rerender } = renderHook(({ target }) => useEngineSession({ createWorker, saveTarget: target, onSaved }), { initialProps: { target: first } })
    let opening
    act(() => { opening = result.current.actions.open({ name: 'roof.dxf', size: 5 }) })
    rerender({ target: { ...first, scopeKey: 'second' } })
    await act(async () => { late.resolve({ drawing_version: version() }); await opening })
    expect(onSaved).not.toHaveBeenCalled()
    expect(createWorker).not.toHaveBeenCalled()
  })

  it('opens canonical bytes with a UUID version and never treats them as a legacy number', async () => {
    const worker = new WorkerTransport()
    const target = { kind: 'project', scopeKey: 'first', readVersion: async () => ({ bytes, versionId: PARENT, documentId: 'roof.dxf' }) }
    const { result } = renderHook(() => useEngineSession({ createWorker: () => worker, saveTarget: target }))
    await act(async () => { await result.current.actions.openProjectVersion(PARENT) })
    expect(worker.posted.some(message => message.type === 'loadDocument' && message.documentId === 'roof.dxf')).toBe(true)
    act(() => { worker.listeners.message.forEach(fn => fn({ data: { type: 'documentLoaded', documentId: 'roof.dxf', entities: [], entityCount: 0, unsupported: [] } })) })
    expect(result.current.committedVersion).toBe(PARENT)
  })

  it('drops a delayed same-filename parse from a previously opened canonical version', async () => {
    const workers = []
    const target = { kind: 'project', scopeKey: 'first', readVersion: async versionId => ({ bytes, versionId, documentId: 'roof.dxf' }) }
    const { result } = renderHook(() => useEngineSession({ createWorker: () => { const worker = new WorkerTransport(); workers.push(worker); return worker }, saveTarget: target }))
    await act(async () => { await result.current.actions.openProjectVersion(PARENT) })
    await act(async () => { await result.current.actions.openProjectVersion(NEXT) })
    expect(workers).toHaveLength(2)
    expect(workers[0].terminated).toBe(true)
    const oldEntity = { id: 'old', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }
    act(() => { workers[0].listeners.message.forEach(fn => fn({ data: { type: 'documentLoaded', documentId: 'roof.dxf', entities: [oldEntity], entityCount: 1, unsupported: [] } })) })
    expect(result.current.entities).toEqual([])
    act(() => { workers[1].listeners.message.forEach(fn => fn({ data: { type: 'documentLoaded', documentId: 'roof.dxf', entities: [], entityCount: 0, unsupported: [] } })) })
    expect(result.current.committedVersion).toBe(NEXT)
  })

  it('canonical-save keeps the canonical successor identity and exact edited bytes', async () => {
    const worker = new WorkerTransport()
    const saved = await receipt()
    const onSaved = vi.fn()
    const target = { kind: 'project', scopeKey: 'first', headVersion: PARENT,
      readVersion: async versionId => ({ bytes, versionId, documentId: 'roof.dxf' }), save: vi.fn(async () => saved) }
    const { result } = renderHook(() => useEngineSession({ createWorker: () => worker, saveTarget: target, onSaved }))
    await act(async () => { await result.current.actions.openProjectVersion(PARENT) })
    const line = { id: 'panel', type: 'LINE', layer: 'Panels', vertices: [[120, 120], [159.6, 120]] }
    act(() => { worker.listeners.message.forEach(fn => fn({ data: { type: 'documentLoaded', documentId: 'roof.dxf', entities: [line], entityCount: 1, unsupported: [] } })) })
    act(() => { result.current.actions.select('panel') })
    act(() => { result.current.actions.applyEdit('move', { dx: '12', dy: '6' }) })
    expect(worker.posted.some(message => message.type === 'applyEdit' && message.op === 'move')).toBe(true)
    const moved = { ...line, vertices: [[132, 126], [171.6, 126]] }
    act(() => { worker.listeners.message.forEach(fn => fn({ data: { type: 'editApplied', op: 'move', ok: true, entities: [moved], entityCount: 1, bytes, byteLength: bytes.length } })) })
    await act(async () => { await result.current.actions.save() })
    expect(target.save.mock.calls[0][1]).toBe(PARENT)
    expect(Array.from(target.save.mock.calls[0][0])).toEqual(Array.from(bytes))
    expect(result.current.committedVersion).toBe(NEXT)
    expect(result.current.savedVersion).toBe(NEXT)
    expect(onSaved).toHaveBeenCalledWith(saved)
  })
})
