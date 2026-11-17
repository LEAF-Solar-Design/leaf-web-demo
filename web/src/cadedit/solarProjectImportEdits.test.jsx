import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useEngineSession from './engineSession.js'

const INPUT = () => new Uint8Array([1, 2, 3])
const EDITED = () => new Uint8Array([4, 5, 6])
const line = (x = 0) => ({ id: '1', type: 'LINE', layer: '0', vertices: [[x, 0, 0], [x + 1, 0, 0]] })
const file = () => new File([new Uint8Array([99])], 'replacement.dxf', { type: 'application/dxf' })
function mount() {
  const listeners = new Map()
  const worker = { posted: [], addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: type => listeners.delete(type), postMessage(message) { this.posted.push(message) },
    terminate: vi.fn(), emit(message) { act(() => listeners.get('message')?.({ data: message })) } }
  const receipt = { drawing_version: { version_id: 'next-version' } }
  const openFile = vi.fn(async () => receipt)
  const onSaved = vi.fn()
  const saveTarget = { kind: 'project', scopeKey: 'public-project', openFile }
  const hook = renderHook(() => useEngineSession({ createWorker: () => worker, saveTarget, onSaved }))
  return { ...hook, worker, receipt, openFile, onSaved }
}
function loaded(h, x = 0) {
  h.worker.emit({ type: 'documentLoaded', documentId: 'trusted.dxf', entities: [line(x)], entityCount: 1, unsupported: [] })
}
function opened(h) {
  act(() => h.result.current.actions.openBytes(INPUT(), 'trusted.dxf', { committed: true, version: 1 }))
  loaded(h)
  act(() => h.result.current.actions.select('1'))
}
function edit(h) {
  act(() => h.result.current.actions.applyEdit('move', { dx: '1', dy: '0' }))
  h.worker.emit({ type: 'editApplied', op: 'move', ok: true, entities: [line(1)], entityCount: 1,
    bytes: EDITED(), byteLength: 3 })
}
afterEach(() => cleanup())

it('refuses project import before upload while preserving unsaved native bytes and Undo/Redo', async () => {
  const h = mount(); opened(h); edit(h)
  const before = h.result.current
  await act(async () => { await h.result.current.actions.open(file()) })
  expect(h.openFile).not.toHaveBeenCalled()
  expect(h.onSaved).not.toHaveBeenCalled()
  expect(h.result.current.documentId).toBe(before.documentId)
  expect(h.result.current.savedBytes).toBe(before.savedBytes)
  expect(h.result.current.entities).toBe(before.entities)
  expect(h.result.current.undoDepth).toBe(1)
  expect(h.result.current.busy).toBe(false)
  expect(h.result.current.status).toMatch(/save|download/i)
  expect(h.worker.terminate).not.toHaveBeenCalled()
  act(() => expect(h.result.current.actions.undo()).toBe(true)); loaded(h)
  expect(h.worker.posted.at(-1).bytes).toEqual(INPUT())
  act(() => expect(h.result.current.actions.redo()).toBe(true)); loaded(h, 1)
  expect(h.result.current.savedBytes).toEqual(EDITED())
})

it('refuses same-tick import while an edit is pending and allows import after a matching refusal', async () => {
  const h = mount(); opened(h)
  await act(async () => {
    h.result.current.actions.applyEdit('move', { dx: '1', dy: '0' })
    await h.result.current.actions.open(file())
  })
  expect(h.openFile).not.toHaveBeenCalled()
  expect(h.onSaved).not.toHaveBeenCalled()
  expect(h.result.current.busy).toBe(true)
  h.worker.emit({ type: 'editApplied', op: 'move', ok: false, reason: 'synthetic refusal' })
  expect(h.result.current.busy).toBe(false)
  await act(async () => expect(await h.result.current.actions.open(file())).toBe(h.receipt))
  expect(h.openFile).toHaveBeenCalledTimes(1)
  expect(h.onSaved).toHaveBeenCalledWith(h.receipt)
})

it.each(['empty', 'clean', 'undone'])('keeps normal project import available for an %s session', async state => {
  const h = mount()
  if (state !== 'empty') opened(h)
  if (state === 'undone') {
    edit(h); act(() => expect(h.result.current.actions.undo()).toBe(true)); loaded(h)
    expect(h.result.current.dirty).toBe(false)
  }
  const replacement = file()
  await act(async () => expect(await h.result.current.actions.open(replacement)).toBe(h.receipt))
  expect(h.openFile).toHaveBeenCalledWith(replacement)
  expect(h.openFile).toHaveBeenCalledTimes(1)
  expect(h.onSaved).toHaveBeenCalledWith(h.receipt)
})
