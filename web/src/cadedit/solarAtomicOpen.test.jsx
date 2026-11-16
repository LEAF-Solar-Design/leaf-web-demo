import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useEngineSession, { SESSION_ERROR } from './engineSession.js'
import { EngineBoundary } from '../cad/engineWorker.js'

const INPUT = () => new Uint8Array([10, 20, 30])
const EDITED = () => new Uint8Array([40, 50, 60])
const line = (x = 0) => ({ id: '1', type: 'LINE', layer: 'Panels', vertices: [[x, 0], [x + 1, 1]] })
function mount() {
  const workers = []
  const hook = renderHook(() => useEngineSession({ createWorker: () => {
    const listeners = new Map()
    const worker = { posted: [], terminated: false,
      addEventListener(type, listener) { listeners.set(type, listener) },
      removeEventListener(type) { listeners.delete(type) },
      postMessage(message) { worker.posted.push(message) },
      terminate() { worker.terminated = true },
      emit(message) { act(() => listeners.get('message')?.({ data: message })) },
    }
    workers.push(worker)
    return worker
  } }))
  return { ...hook, workers, get worker() { return workers.at(-1) } }
}
function loaded(s, documentId = 'trusted.dxf', x = 0, extra = {}) {
  s.worker.emit({ type: 'documentLoaded', documentId, entities: [line(x)], entityCount: 1, unsupported: [], ...extra })
}
function edited(s) {
  act(() => s.result.current.actions.openBytes(INPUT(), 'trusted.dxf', { committed: true, version: 1 }))
  loaded(s)
  act(() => s.result.current.actions.select('1'))
  act(() => s.result.current.actions.applyEdit('move', { dx: '1', dy: '0' }))
  s.worker.emit({ type: 'editApplied', op: 'move', ok: true, entities: [line(1)], entityCount: 1, bytes: EDITED() })
}
function assertRestored(s, before) {
  expect(s.result.current.documentId).toBe('trusted.dxf')
  expect(s.result.current.entities).toEqual(before.entities)
  expect(s.result.current.savedBytes).toEqual(before.savedBytes)
  expect(s.result.current.committedVersion).toBe(1)
  expect(s.result.current.undoDepth).toBe(1)
  expect(s.result.current.redoDepth).toBe(0)
  expect(s.result.current.engineParsed).toBe(true)
  expect(s.result.current.busy).toBe(false)
  expect(s.result.current.status).toMatch(/previous drawing is still open/i)
}
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it.each(['error', 'refusal'])('keeps edited bytes and real Undo/Redo state after a replacement %s', (kind) => {
  const s = mount(); edited(s); const before = s.result.current
  act(() => s.result.current.actions.openBytes(new Uint8Array([99]), 'bad.dxf'))
  if (kind === 'error') s.worker.emit({ type: 'error', message: 'parse_failed:synthetic' })
  else loaded(s, 'bad.dxf', 0, { refusal: 'synthetic read-only refusal' })
  expect(s.result.current.busy).toBe(true)
  expect(s.worker.posted.at(-1)).toEqual({ type: 'loadDocument', documentId: 'trusted.dxf', bytes: before.savedBytes })
  loaded(s, 'trusted.dxf', 1)
  assertRestored(s, before)
  expect(s.result.current.errorKind).toBe(kind === 'error' ? SESSION_ERROR.ENGINE : SESSION_ERROR.REFUSED)
  act(() => expect(s.result.current.actions.undo()).toBe(true))
  expect(s.worker.posted.at(-1).bytes).toEqual(INPUT()); loaded(s)
  act(() => expect(s.result.current.actions.redo()).toBe(true))
  expect(s.worker.posted.at(-1).bytes).toEqual(EDITED()); loaded(s, 'trusted.dxf', 1)
  expect(s.result.current.savedBytes).toEqual(EDITED())
})

it('preserves an existing redo branch across failed replacement', () => {
  const s = mount(); edited(s)
  act(() => s.result.current.actions.undo()); loaded(s)
  expect(s.result.current.redoDepth).toBe(1)
  act(() => s.result.current.actions.openBytes(new Uint8Array([99]), 'bad.dxf'))
  s.worker.emit({ type: 'error', message: 'parse_failed' }); loaded(s)
  expect(s.result.current.redoDepth).toBe(1)
  act(() => expect(s.result.current.actions.redo()).toBe(true))
  expect(s.worker.posted.at(-1).bytes).toEqual(EDITED()); loaded(s, 'trusted.dxf', 1)
})

it('keeps successful replacement as a new history floor', () => {
  const s = mount(); edited(s)
  act(() => s.result.current.actions.openBytes(new Uint8Array([77]), 'new.dxf'))
  loaded(s, 'new.dxf', 7)
  expect(s.result.current.documentId).toBe('new.dxf')
  expect(s.result.current.entities).toEqual([line(7)])
  expect(s.result.current.undoDepth).toBe(0)
  expect(s.result.current.redoDepth).toBe(0)
  expect(s.result.current.savedBytes).toBeNull()
  act(() => expect(s.result.current.actions.undo()).toBe(false))
})

it('does not replace a document while its edit is pending', () => {
  const s = mount(); edited(s)
  act(() => s.result.current.actions.applyEdit('move', { dx: '1', dy: '0' }))
  const posted = s.worker.posted.length
  act(() => expect(s.result.current.actions.openBytes(INPUT(), 'bad.dxf')).toBeNull())
  expect(s.worker.posted).toHaveLength(posted)
  expect(s.result.current.documentId).toBe('trusted.dxf')
})

it('drops pending restoration on reset and ignores old-generation replies', () => {
  const s = mount(); edited(s); const old = s.worker
  act(() => s.result.current.actions.openBytes(new Uint8Array([99]), 'bad.dxf'))
  old.emit({ type: 'error', message: 'parse_failed' })
  act(() => s.result.current.actions.reset())
  act(() => s.result.current.actions.openBytes(INPUT(), 'fresh.dxf')); loaded(s, 'fresh.dxf')
  old.emit({ type: 'documentLoaded', documentId: 'trusted.dxf', entities: [line(99)], entityCount: 1 })
  expect(old.terminated).toBe(true)
  expect(s.result.current.documentId).toBe('fresh.dxf')
  expect(s.result.current.savedBytes).toBeNull()
  expect(s.result.current.undoDepth).toBe(0)
})

it('keeps bytes for download and blocks save if trusted-byte restoration also fails', () => {
  const s = mount(); edited(s)
  act(() => s.result.current.actions.openBytes(new Uint8Array([99]), 'bad.dxf'))
  s.worker.emit({ type: 'error', message: 'parse_failed' })
  s.worker.emit({ type: 'error', message: 'restore_failed' })
  expect(s.result.current.savedBytes).toEqual(EDITED())
  expect(s.result.current.entities).toEqual([])
  expect(s.result.current.engineParsed).toBe(false)
  expect(s.result.current.undoDepth).toBe(0)
  expect(s.result.current.busy).toBe(false)
  expect(s.result.current.status).toMatch(/Download its retained bytes/i)
})

it.each(['false', 'throw'])('keeps the prior state when posting replacement returns %s', (kind) => {
  const s = mount(); edited(s); const before = s.result.current
  const original = EngineBoundary.prototype.post
  vi.spyOn(EngineBoundary.prototype, 'post').mockImplementation(function (message) {
    if (message.type === 'loadDocument' && message.documentId === 'bad.dxf') {
      if (kind === 'throw') throw new DOMException('synthetic', 'DataCloneError')
      return false
    }
    return original.call(this, message)
  })
  act(() => s.result.current.actions.openBytes(INPUT(), 'bad.dxf'))
  expect(s.result.current.entities).toEqual(before.entities)
  expect(s.result.current.savedBytes).toEqual(before.savedBytes)
  expect(s.result.current.undoDepth).toBe(1)
  expect(s.result.current.engineParsed).toBe(true)
  expect(s.result.current.busy).toBe(false)
})

it('restores the pre-read document after an invalid File import', async () => {
  const s = mount(); edited(s); const before = s.result.current
  await act(async () => s.result.current.actions.open({ name: 'bad.dxf', size: 1, arrayBuffer: async () => new Uint8Array([99]).buffer }))
  s.worker.emit({ type: 'error', message: 'parse_failed' }); loaded(s, 'trusted.dxf', 1)
  assertRestored(s, before)
})
