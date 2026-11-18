// S01 session fault boundary. Scripted transport, not a WASM producer proof.
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useEngineSession, { SESSION_ERROR } from './engineSession.js'

afterEach(cleanup)

class ScriptedWorker {
  constructor() { this.listeners = { message: [], error: [], messageerror: [] }; this.terminated = false; this.posted = [] }
  addEventListener(type, listener) { this.listeners[type]?.push(listener) }
  removeEventListener(type, listener) { this.listeners[type] = this.listeners[type].filter(row => row !== listener) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => this.listeners.message.forEach(listener => listener({ data }))) }
}

const original = new Uint8Array([48, 10, 69, 79, 70, 10])
const edited = new Uint8Array([1, 2, 3])
const line = { id: 'line', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [2, 1]] }
const moved = { ...line, vertices: [[1, 0], [3, 1]] }
const success = () => ({ type: 'editApplied', op: 'move', ok: true, entities: [moved], entityCount: 1,
  bytes: Uint8Array.from(edited), byteLength: edited.length })

function open(result, worker) {
  act(() => result.current.actions.openBytes(Uint8Array.from(original), 'synthetic.dxf', { committed: true }))
  const active = typeof worker === 'function' ? worker() : worker
  active.emit({ type: 'documentLoaded', documentId: 'synthetic.dxf', entities: [line], entityCount: 1 })
  act(() => result.current.actions.select(line.id))
}

function move(result) {
  act(() => result.current.actions.applyEdit('move', { dx: '1', dy: '0' }))
}

it.each(['missing-buffer', 'nonfinite-geometry', 'mismatched-operation'])(
  'invalidates an untrusted %s success without losing trusted bytes, then reopens cleanly', async mode => {
    const workers = []
    const save = vi.fn(async () => ({ new_version: { version: 2 } }))
    const saveTarget = { headVersion: 1, save }
    const { result, unmount } = renderHook(() => useEngineSession({ saveTarget, createWorker: () => {
      const worker = new ScriptedWorker(); workers.push(worker); return worker
    } }))
    try {
      open(result, () => workers.at(-1))
      const worker = workers[0]
      move(result); worker.emit(success())
      expect(result.current.undoDepth).toBe(1)
      await act(async () => { await result.current.actions.save() })
      expect(save).toHaveBeenCalledOnce()
      expect(result.current.committedBytes).toEqual(edited)
      move(result)
      worker.emit({ ...success(), bytes: new Uint8Array([4, 5, 6]) })
      const trusted = Uint8Array.from(result.current.savedBytes)
      move(result)
      const reply = success()
      if (mode === 'missing-buffer') { delete reply.bytes; delete reply.byteLength }
      if (mode === 'nonfinite-geometry') reply.entities = [{ ...moved, vertices: [[NaN, 0], [3, 1]] }]
      if (mode === 'mismatched-operation') reply.op = 'rotate'
      worker.emit(reply)
      expect(worker.terminated).toBe(true)
      expect(result.current.busy).toBe(false)
      expect(result.current.errorKind).toBe(SESSION_ERROR.TRANSPORT)
      expect(result.current.engineParsed).toBe(false)
      expect(result.current.entities).toEqual([])
      expect(result.current.undoDepth).toBe(0)
      expect(result.current.redoDepth).toBe(0)
      expect(result.current.savedBytes).toEqual(trusted)
      expect(result.current.committedBytes).toEqual(edited)
      await act(async () => { expect(await result.current.actions.save()).toBeNull() })
      expect(save).toHaveBeenCalledOnce()
      worker.emit(success())
      expect(result.current.engineParsed).toBe(false)
      expect(result.current.savedBytes).toEqual(trusted)
      open(result, () => workers.at(-1))
      expect(workers).toHaveLength(2)
      move(result); workers[1].emit(success())
      expect(result.current.errorKind).toBeNull()
      expect(result.current.engineParsed).toBe(true)
      expect(result.current.undoDepth).toBe(1)
    } finally { unmount() }
  })

it('keeps a valid matching refusal recoverable without replacing the worker', () => {
  const worker = new ScriptedWorker()
  const { result, unmount } = renderHook(() => useEngineSession({ createWorker: () => worker }))
  try {
    open(result, worker); move(result)
    worker.emit({ type: 'editApplied', op: 'move', ok: false, reason: 'locked_layer' })
    expect(result.current.busy).toBe(false)
    expect(result.current.errorKind).toBe(SESSION_ERROR.REFUSED)
    expect(result.current.engineParsed).toBe(true)
    expect(worker.terminated).toBe(false)
    move(result); worker.emit(success())
    expect(result.current.errorKind).toBeNull()
    expect(result.current.undoDepth).toBe(1)
  } finally { unmount() }
})

it('keeps the first edit pending when two actions run before React renders', () => {
  const worker = new ScriptedWorker()
  const { result, unmount } = renderHook(() => useEngineSession({ createWorker: () => worker }))
  try {
    open(result, worker)
    act(() => {
      result.current.actions.applyEdit('move', { dx: '1', dy: '0' })
      result.current.actions.applyEdit('rotate', { cx: '0', cy: '0', deg: '30' })
    })
    expect(worker.posted.filter(row => row.type === 'applyEdit')).toHaveLength(1)
    expect(result.current.busy).toBe(true)
    worker.emit(success())
    expect(result.current.errorKind).toBeNull()
    expect(result.current.undoDepth).toBe(1)
  } finally { unmount() }
})
