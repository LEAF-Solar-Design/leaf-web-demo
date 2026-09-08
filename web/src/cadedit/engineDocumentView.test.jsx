// W4f slice A0: the engine document reaches the canvas through the viewer's
// own applyVersion seam while a DXF is open, and the console drawing comes
// back when it closes, the worker dies, or the surface unmounts.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CadEditSurface from './CadEditSurface.jsx'
import EngineDocumentView from './EngineDocumentView.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
  die() { act(() => { this.listeners.get('error')?.({ message: 'worker died' }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0, 0], [100, 50, 0]], radius: null, startDeg: null, endDeg: null }
const CIRCLE = { id: 'e2', type: 'CIRCLE', layer: '0', vertices: [[3, 3, 0]], radius: 1.5, startDeg: null, endDeg: null }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
let viewer
let viewerRef
let onShown
let sessionActions
function SelectionProbe() {
  const { session } = useEngineSessionContext()
  sessionActions = session.actions
  return <output data-testid="engine-selection">{session.selectedId ?? 'none'}</output>
}

function mount(viewProps = {}) {
  workers = []
  viewer = { applyVersion: vi.fn() }
  viewerRef = { current: viewer }
  onShown = vi.fn()
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  const tree = (props) => (
    <EngineSessionProvider createWorker={createWorker}>
      <EngineDocumentView viewerRef={viewerRef} onShown={onShown} {...props} />
      <CadEditSurface enabled />
      <SelectionProbe />
    </EngineSessionProvider>
  )
  const utils = render(tree(viewProps))
  return { ...utils, rerender: (props) => utils.rerender(tree(props)) }
}

async function openAndLoad(entities, name = 'one.dxf') {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf(name)] } })
    await Promise.resolve()
    await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBeGreaterThan(0))
  workers[workers.length - 1].emit({ type: 'documentLoaded', documentId: name, entities, entityCount: entities.length, unsupported: [] })
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe.each([true, false])('EngineDocumentView reverse selection (callback enabled: %s)', (withCallback) => {
  const entities = [{ ...LINE, id: '42' }, { ...CIRCLE, id: '43' }]
  function setup() {
    const callback = vi.fn()
    const props = withCallback ? { onSelectedHandleChange: callback } : {}
    return { ...mount(props), callback, props }
  }

  it('mirrors engine selection once and ignores the parent echo without selecting again', async () => {
    const { rerender, callback, props } = setup()
    await openAndLoad(entities)
    act(() => { sessionActions.select('42') })
    expect(screen.getByTestId('engine-selection').textContent).toBe('42')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A']] : [])
    const select = vi.spyOn(sessionActions, 'select')
    rerender({ ...props, selectedHandle: '2A' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('42')
    expect(select).toHaveBeenCalledTimes(withCallback ? 0 : 1)
    expect(callback.mock.calls).toEqual(withCallback ? [['2A']] : [])
  })

  it('does not call back for the forward mirror alone', async () => {
    const { rerender, callback, props } = setup()
    await openAndLoad(entities)
    rerender({ ...props, selectedHandle: '2B' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('43')
    expect(callback).not.toHaveBeenCalled()
  })

  it('mirrors an explicit clear once', async () => {
    const { callback } = setup()
    await openAndLoad(entities)
    act(() => { sessionActions.select('42') })
    expect(screen.getByTestId('engine-selection').textContent).toBe('42')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A']] : [])
    act(() => { sessionActions.select(null) })
    expect(screen.getByTestId('engine-selection').textContent).toBe('none')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A'], [null]] : [])
    act(() => { sessionActions.select(null) })
    expect(screen.getByTestId('engine-selection').textContent).toBe('none')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A'], [null]] : [])
  })

  it('does not mirror the initial empty selection on mount or load', async () => {
    const { callback } = setup()
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback).not.toHaveBeenCalled()
    await openAndLoad(entities)
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback).not.toHaveBeenCalled()
  })

  it('does not mirror a session selection before documentLoaded', () => {
    const { callback } = setup()
    act(() => { sessionActions.select('42') })
    expect(screen.getByTestId('engine-selection').textContent).toBe('42')
    expect(callback).not.toHaveBeenCalled()
  })

  it('clears a hidden document selection once after a failed load and mirrors the next document', async () => {
    const { rerender, callback, props } = setup()
    await openAndLoad(entities)
    act(() => { sessionActions.select('42') })
    expect(callback.mock.calls).toEqual(withCallback ? [['2A']] : [])
    if (withCallback) rerender({ ...props, selectedHandle: '2A' })
    act(() => { sessionActions.openBytes(new Uint8Array([48]), 'malformed.dxf') })
    workers[0].emit({ type: 'error', message: 'DXF parse failed' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A'], [null]] : [])
    if (withCallback) rerender({ ...props, selectedHandle: null })
    await openAndLoad([entities[1]], 'two.dxf')
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A'], [null]] : [])
    act(() => { sessionActions.select('43') })
    expect(screen.getByTestId('engine-selection').textContent).toBe('43')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A'], [null], ['2B']] : [])
  })

  it('does not replay a forward selection after a failed load and recovery', async () => {
    const { rerender, callback, props } = setup()
    await openAndLoad(entities)
    rerender({ ...props, selectedHandle: '2A' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('42')
    expect(callback).not.toHaveBeenCalled()
    const select = vi.spyOn(sessionActions, 'select')
    act(() => { sessionActions.openBytes(new Uint8Array([48]), 'malformed.dxf') })
    workers[0].emit({ type: 'error', message: 'DXF parse failed' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback.mock.calls).toEqual(withCallback ? [[null]] : [])
    if (withCallback) rerender({ ...props, selectedHandle: null })
    await openAndLoad([{ ...entities[0] }], 'two.dxf')
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback.mock.calls).toEqual(withCallback ? [[null]] : [])
    expect(select).not.toHaveBeenCalled()
  })

  it('does not call back when a document hides without a held selection', async () => {
    const { callback } = setup()
    await openAndLoad(entities)
    act(() => { sessionActions.openBytes(new Uint8Array([48]), 'malformed.dxf') })
    workers[0].emit({ type: 'error', message: 'DXF parse failed' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback).not.toHaveBeenCalled()
  })

  it('mirrors a selection lost through an edit reply to null exactly once', async () => {
    const { rerender, callback, props } = setup()
    await openAndLoad(entities)
    act(() => { sessionActions.select('42') })
    expect(screen.getByTestId('engine-selection').textContent).toBe('42')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A']] : [])
    // App stores each callback value in selectedHandle before the next edit.
    if (withCallback) rerender({ ...props, selectedHandle: '2A' })
    const select = vi.spyOn(sessionActions, 'select')
    const reply = { type: 'editApplied', op: 'delete', ok: true, entities: [entities[1]], entityCount: 1, bytes: new Uint8Array([48]), byteLength: 1 }
    workers[0].emit(reply)
    if (withCallback) rerender({ ...props, selectedHandle: null })
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A'], [null]] : [])
    workers[0].emit({ ...reply, entities: [entities[1]] })
    expect(screen.getByTestId('engine-selection').textContent).toBe('')
    expect(callback.mock.calls).toEqual(withCallback ? [['2A'], [null]] : [])
    expect(select).not.toHaveBeenCalled()
  })
})

describe('EngineDocumentView (W4f slice A0)', () => {
  it('mirrors changed hex handles to decimal engine ids and clears on an empty canvas click', async () => {
    const utils = mount()
    await openAndLoad([{ ...LINE, id: '10' }, { ...CIRCLE, id: '16' }])
    utils.rerender({ selectedHandle: 'A' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('10')
    utils.rerender({ selectedHandle: '10' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('16')
    utils.rerender({ selectedHandle: null })
    expect(screen.getByTestId('engine-selection').textContent).toBe('none')
  })

  it('preserves a workbench radio selection through initial and unchanged null handles', async () => {
    const utils = mount({ selectedHandle: null })
    await openAndLoad([{ ...LINE, id: '10' }, { ...CIRCLE, id: '16' }])
    fireEvent.click(screen.getByRole('radio', { name: /CIRCLE on layer/ }))
    expect(screen.getByTestId('engine-selection').textContent).toBe('16')
    utils.rerender({ selectedHandle: null })
    expect(screen.getByTestId('engine-selection').textContent).toBe('16')
    utils.rerender({ selectedHandle: null })
    expect(screen.getByTestId('engine-selection').textContent).toBe('16')
  })

  it('leaves the engine selection unchanged for an unknown handle', async () => {
    const utils = mount()
    await openAndLoad([{ ...LINE, id: '10' }, { ...CIRCLE, id: '16' }])
    fireEvent.click(screen.getByRole('radio', { name: /CIRCLE on layer/ }))
    utils.rerender({ selectedHandle: 'FFFF' })
    expect(screen.getByTestId('engine-selection').textContent).toBe('16')
  })

  it('ignores handle changes before documentLoaded and does not replay the pending handle after loading', async () => {
    const utils = mount()
    const before = screen.getByTestId('engine-selection').textContent
    utils.rerender({ selectedHandle: 'A' })
    expect(screen.getByTestId('engine-selection').textContent).toBe(before)
    await openAndLoad([{ ...LINE, id: '10' }, { ...CIRCLE, id: '16' }])
    expect(screen.getByTestId('engine-selection').textContent).toBe(before)
  })

  it('shows the engine document once loaded, re-shows on every edit, and never touches the viewer before that', async () => {
    mount()
    expect(viewer.applyVersion).not.toHaveBeenCalled()
    await openAndLoad([LINE, CIRCLE])
    expect(viewer.applyVersion).toHaveBeenCalledTimes(1)
    const intake = viewer.applyVersion.mock.calls[0][0]
    expect(intake.source).toBe('engine')
    expect(intake.documentId).toBe('one.dxf')
    expect(intake.polylines.map((p) => p.handle)).toEqual(['e1', 'e2'])
    expect(intake.polylines[1].closed).toBe(true)
    expect(onShown).toHaveBeenCalledWith(intake)
    // An edit re-parses: a new entity list, a new intake; the same list never re-applies.
    workers[0].emit({ type: 'editApplied', op: 'delete', ok: true, entities: [CIRCLE], entityCount: 1, bytes: new Uint8Array([48]), byteLength: 1 })
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
    expect(viewer.applyVersion.mock.calls[1][0].polylines.map((p) => p.handle)).toEqual(['e2'])
  })

  it('hands the console drawing back (null) when the worker dies, and again on unmount only if something was shown', async () => {
    const utils = mount()
    await openAndLoad([LINE])
    expect(viewer.applyVersion).toHaveBeenCalledTimes(1)
    workers[0].die()
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
    expect(viewer.applyVersion.mock.calls[1][0]).toBeNull()
    expect(onShown).toHaveBeenLastCalledWith(null)
    utils.unmount()
    // Nothing shown at unmount time: no third call.
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
  })

  it('restores the console drawing on unmount while a document is showing, and tolerates a viewer without applyVersion', async () => {
    const utils = mount()
    await openAndLoad([LINE])
    utils.unmount()
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
    expect(viewer.applyVersion.mock.calls[1][0]).toBeNull()
    // The host hears the stamp is gone too (no lingering data-engine-document).
    expect(onShown).toHaveBeenLastCalledWith(null)
    // A ref with no viewer yet: nothing thrown, nothing shown.
    workers = []
    viewer = {}
    viewerRef = { current: null }
    const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
    render(
      <EngineSessionProvider createWorker={createWorker}>
        <EngineDocumentView viewerRef={viewerRef} />
        <CadEditSurface enabled />
      </EngineSessionProvider>,
    )
    await openAndLoad([LINE], 'two.dxf')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('1')
  })
})
