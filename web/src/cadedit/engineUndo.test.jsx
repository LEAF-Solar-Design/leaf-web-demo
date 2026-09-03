// W4f slice F: undo/redo of engine edits as a bytes-snapshot stack. Every
// applied edit hands back the whole written document, so "before this edit"
// is exactly the bytes that were current then, and stepping is a re-load of
// those bytes through the same loadDocument path the open uses.
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import useEngineSession, { GEOMETRY_SOURCE, MAX_UNDO_BYTES, SESSION_ERROR } from './engineSession.js'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = { message: [], error: [], messageerror: [] }; this.terminated = false }
  addEventListener(type, cb) { (this.listeners[type] ||= []).push(cb) }
  removeEventListener(type, cb) { this.listeners[type] = (this.listeners[type] || []).filter((x) => x !== cb) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.message.forEach((cb) => cb({ data })) }) }
  die() { act(() => { this.listeners.error.forEach((cb) => cb({ type: 'error' })) }) }
  lastPost() { return this.posted[this.posted.length - 1] }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }
const POLY = { id: 'e2', type: 'LWPOLYLINE', layer: 'Outline', vertices: [[0, 0], [1, 0], [1, 1]] }
const loaded = (entities, documentId = 'one.dxf') => ({ type: 'documentLoaded', documentId, entities, entityCount: entities.length, unsupported: [] })
const edited = (op, entities, bytes) => ({ type: 'editApplied', op, ok: true, entities, entityCount: entities.length, bytes, byteLength: bytes.length })

const ORIGINAL_TEXT = '0\nEOF\n'
function fileOf(name = 'one.dxf', text = ORIGINAL_TEXT) {
  const bytes = new TextEncoder().encode(text)
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

function mountSession() {
  const workers = []
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  const handle = { current: null, workers }
  function Host() { handle.current = useEngineSession({ createWorker }); return null }
  const view = render(<Host />)
  handle.unmount = view.unmount
  return handle
}

async function openAndLoad(session, entities = [LINE]) {
  await act(async () => { await session.current.actions.open(fileOf()) })
  session.workers[session.workers.length - 1].emit(loaded(entities))
}

// An action's return value, with its state update flushed.
function stepped(fn) {
  let result
  act(() => { result = fn() })
  return result
}

function editOnce(session, op, entities, bytes) {
  act(() => session.current.actions.select(entities[0]?.id || 'e1'))
  act(() => session.current.actions.applyEdit(op, { dx: '1', dy: '0', vertexIndex: '0', layer: 'L' }))
  session.workers[0].emit(edited(op, entities, bytes))
}

beforeEach(() => { vi.spyOn(crypto.subtle, 'digest').mockResolvedValue(new Uint8Array(32).buffer) })
afterEach(() => { vi.restoreAllMocks() })

describe('engine undo (W4f slice F)', () => {
  it('starts with nothing to step to, counts each applied edit, and undo re-loads the bytes before it', async () => {
    const session = mountSession()
    await openAndLoad(session, [LINE, POLY])
    expect(session.current.undoDepth).toBe(0)
    expect(session.current.redoDepth).toBe(0)
    expect(stepped(() => session.current.actions.undo())).toBe(false)
    const b1 = new Uint8Array([1, 1])
    const b2 = new Uint8Array([2, 2])
    editOnce(session, 'move', [{ ...LINE, vertices: [[1, 0], [2, 1]] }, POLY], b1)
    editOnce(session, 'setLayer', [{ ...LINE, layer: 'L' }, POLY], b2)
    expect(session.current.undoDepth).toBe(2)
    expect(session.current.savedBytes).toBe(b2)
    // Undo: the load that goes to the engine carries the bytes BEFORE the
    // last edit (b1), under the same document id, and the store is busy.
    expect(stepped(() => session.current.actions.undo())).toBe(true)
    expect(session.current.busy).toBe(true)
    expect(session.current.undoDepth).toBe(1)
    expect(session.current.redoDepth).toBe(1)
    const post = session.workers[0].lastPost()
    expect(post.type).toBe('loadDocument')
    expect(post.documentId).toBe('one.dxf')
    expect(post.bytes).toBe(b1)
    // A second undo while the first is in flight is refused.
    expect(stepped(() => session.current.actions.undo())).toBe(false)
    session.workers[0].emit(loaded([{ ...LINE, vertices: [[1, 0], [2, 1]] }, POLY]))
    expect(session.current.busy).toBe(false)
    expect(session.current.status).toBe('Undid setLayer: 2 entities, 2 bytes.')
    expect(session.current.savedBytes).toBe(b1)
    expect(session.current.geometrySource).toBe(GEOMETRY_SOURCE.ENGINE_REPARSE)
    expect(session.current.selectedId).toBe('e1')
    // Undo again: back to the opened file, which counts as unedited.
    expect(stepped(() => session.current.actions.undo())).toBe(true)
    const back = session.workers[0].lastPost()
    expect(new TextDecoder().decode(back.bytes)).toBe(ORIGINAL_TEXT)
    session.workers[0].emit(loaded([LINE, POLY]))
    expect(session.current.savedBytes).toBeNull()
    expect(session.current.undoDepth).toBe(0)
    expect(session.current.redoDepth).toBe(2)
    expect(session.current.status).toBe('Undid move: 2 entities, 6 bytes.')
  })

  it('redo steps forward again, and a new edit after an undo ends the redo branch', async () => {
    const session = mountSession()
    await openAndLoad(session, [LINE])
    const b1 = new Uint8Array([1])
    const b2 = new Uint8Array([2])
    editOnce(session, 'move', [LINE], b1)
    act(() => { session.current.actions.undo() })
    session.workers[0].emit(loaded([LINE]))
    expect(session.current.redoDepth).toBe(1)
    expect(stepped(() => session.current.actions.redo())).toBe(true)
    expect(session.workers[0].lastPost().bytes).toBe(b1)
    session.workers[0].emit(loaded([LINE]))
    expect(session.current.status).toBe('Redid move: 1 entities, 1 bytes.')
    expect(session.current.savedBytes).toBe(b1)
    expect(session.current.undoDepth).toBe(1)
    expect(session.current.redoDepth).toBe(0)
    act(() => { session.current.actions.undo() })
    session.workers[0].emit(loaded([LINE]))
    expect(session.current.redoDepth).toBe(1)
    editOnce(session, 'setLayer', [LINE], b2)
    expect(session.current.redoDepth).toBe(0)
    expect(session.current.undoDepth).toBe(1)
    expect(stepped(() => session.current.actions.redo())).toBe(false)
  })

  it('a fresh open, a crash and a reset all drop the history; a new document never inherits another\'s snapshots', async () => {
    const session = mountSession()
    await openAndLoad(session, [LINE])
    editOnce(session, 'move', [LINE], new Uint8Array([1]))
    expect(session.current.undoDepth).toBe(1)
    await act(async () => { await session.current.actions.open(fileOf('two.dxf')) })
    session.workers[0].emit(loaded([LINE], 'two.dxf'))
    expect(session.current.undoDepth).toBe(0)
    expect(stepped(() => session.current.actions.undo())).toBe(false)
    editOnce(session, 'move', [LINE], new Uint8Array([3]))
    expect(session.current.undoDepth).toBe(1)
    session.workers[0].die()
    expect(session.current.errorKind).toBe(SESSION_ERROR.CRASHED)
    expect(session.current.undoDepth).toBe(0)
    expect(stepped(() => session.current.actions.undo())).toBe(false)
  })

  it('is bounded by total bytes, dropping the oldest snapshots first', async () => {
    const session = mountSession()
    await openAndLoad(session, [LINE])
    const big = () => new Uint8Array(Math.floor(MAX_UNDO_BYTES / 3) + 1)
    editOnce(session, 'move', [LINE], big())
    editOnce(session, 'move', [LINE], big())
    editOnce(session, 'move', [LINE], big())
    editOnce(session, 'move', [LINE], big())
    // Four edits produced four "before" snapshots (the opened file plus
    // three big ones); only the newest fit under the cap.
    expect(session.current.undoDepth).toBeLessThan(4)
    expect(session.current.undoDepth).toBeGreaterThanOrEqual(1)
  })
})
