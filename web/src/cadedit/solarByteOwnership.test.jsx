import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import useEngineSession, { SESSION_ERROR } from './engineSession.js'

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }
const MOVED = { ...LINE, vertices: [[1, 0], [2, 1]] }
const original = () => new TextEncoder().encode('0\nEOF\n')

function mount(saveTarget = null) {
  const listeners = new Set()
  const worker = {
    posted: [],
    addEventListener(type, callback) { if (type === 'message') listeners.add(callback) },
    removeEventListener(type, callback) { if (type === 'message') listeners.delete(callback) },
    postMessage(message) { worker.posted.push(message) },
    terminate() { listeners.clear() },
    emit(message) { act(() => listeners.forEach(callback => callback({ data: message }))) },
    lastPost() { return worker.posted.at(-1) },
  }
  const hook = renderHook(() => useEngineSession({ createWorker: () => worker, saveTarget }))
  return { worker, result: hook.result }
}

function loaded(session, entities = [LINE]) {
  session.worker.emit({ type: 'documentLoaded', documentId: 'owned.dxf', entities,
    entityCount: entities.length, unsupported: [] })
}

function open(session, bytes) {
  act(() => session.result.current.actions.openBytes(bytes, 'owned.dxf', { committed: true, version: 1 }))
  loaded(session)
}

function move(session, bytes) {
  act(() => session.result.current.actions.select('e1'))
  act(() => session.result.current.actions.applyEdit('move', { dx: '1', dy: '0' }))
  session.worker.emit({ type: 'editApplied', op: 'move', ok: true, entities: [MOVED],
    entityCount: 1, bytes, byteLength: bytes.length })
}

beforeEach(() => vi.stubGlobal('crypto', {
  subtle: { digest: vi.fn(async () => new Uint8Array(32).buffer) },
}))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('retains the loaded view when its caller changes bytes before the first edit', () => {
  const session = mount()
  const input = original()
  const expected = Uint8Array.from(input)
  open(session, input)
  input[0] = 33
  expect(session.worker.posted.find(message => message.type === 'loadDocument').bytes).toEqual(expected)
  move(session, new Uint8Array([1, 2, 3]))
  act(() => { expect(session.result.current.actions.undo()).toBe(true) })
  expect(session.worker.lastPost().bytes).toEqual(expected)
  loaded(session)
  expect(session.result.current.savedBytes).toBeNull()
  expect(session.result.current.dirty).toBe(false)
})

it('retains the edit snapshot when exposed saved bytes are changed before Undo and Redo', () => {
  const session = mount()
  open(session, original())
  const exposed = new Uint8Array([1, 2, 3])
  move(session, exposed)
  expect(session.result.current.savedBytes).toBe(exposed)
  exposed[0] = 99
  act(() => { expect(session.result.current.actions.undo()).toBe(true) })
  loaded(session)
  act(() => { expect(session.result.current.actions.redo()).toBe(true) })
  expect(session.worker.lastPost().bytes).toEqual(new Uint8Array([1, 2, 3]))
  loaded(session, [MOVED])
  expect(session.result.current.savedBytes).toEqual(new Uint8Array([1, 2, 3]))
  expect(exposed).toEqual(new Uint8Array([99, 2, 3]))
})

it('refuses a detached input before changing the session and accepts a later valid load', () => {
  const session = mount()
  const input = original()
  structuredClone(input, { transfer: [input.buffer] })
  expect(input.byteLength).toBe(0)
  act(() => {
    expect(session.result.current.actions.openBytes(input, 'detached.dxf')).toBeNull()
  })
  expect(session.result.current.errorKind).toBe(SESSION_ERROR.REFUSED)
  expect(session.result.current.busy).toBe(false)
  expect(session.worker.posted).toHaveLength(0)
  open(session, original())
  expect(session.result.current.errorKind).toBeNull()
  expect(session.result.current.engineParsed).toBe(true)
})

it('saves the retained edit bytes and seats the same committed revision after caller mutation', async () => {
  const save = vi.fn(async () => ({ new_version: { version: 2 } }))
  const session = mount({ headVersion: 1, save })
  open(session, original())
  const exposed = new Uint8Array([1, 2, 3])
  move(session, exposed)
  exposed[0] = 99
  await act(async () => { await session.result.current.actions.save() })
  expect(save).toHaveBeenCalledOnce()
  expect(save.mock.calls[0][0]).toEqual(new Uint8Array([1, 2, 3]))
  expect(session.result.current.savedBytes).toEqual(new Uint8Array([1, 2, 3]))
  expect(session.result.current.dirty).toBe(false)
  expect(session.result.current.committedVersion).toBe(2)
  move(session, new Uint8Array([4, 5, 6]))
  expect(session.result.current.dirty).toBe(true)
  act(() => { expect(session.result.current.actions.undo()).toBe(true) })
  loaded(session, [MOVED])
  expect(session.worker.lastPost().bytes).toEqual(new Uint8Array([1, 2, 3]))
  expect(session.result.current.dirty).toBe(false)
})
