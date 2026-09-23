// W4f slice A0: the engine document reaches the canvas through the viewer's
// own applyVersion seam while a DXF is open, and the console drawing comes
// back when it closes, the worker dies, or the surface unmounts.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CadEditSurface from './CadEditSurface.jsx'
import EngineDocumentView from './EngineDocumentView.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import * as engineSessionContext from './EngineSessionProvider.jsx'

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
let selectedIds
function SelectionProbe() {
  const { session } = useEngineSessionContext()
  sessionActions = session.actions
  selectedIds = session.selectedIds
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

describe('SSD1-24B canvas picks', () => {
  const entities = [{ ...LINE, id: '10' }, { ...CIRCLE, id: '11' }]
  async function setup() {
    const registerCanvasPick = vi.fn()
    const onSelectedHandleChange = vi.fn()
    const props = { registerCanvasPick, onSelectedHandleChange }
    const view = mount(props)
    await openAndLoad(entities)
    const pick = registerCanvasPick.mock.lastCall[0]
    const click = (handle, additive = true) => {
      let handled
      act(() => { handled = pick(handle, { additive }) })
      return handled
    }
    return { ...view, props, registerCanvasPick, onSelectedHandleChange, click }
  }

  it('SSD1-24B registers once per shown document and unregisters on close and unmount', async () => {
    const view = await setup()
    expect(view.registerCanvasPick).toHaveBeenCalledTimes(1)
    expect(view.registerCanvasPick.mock.lastCall[0]).toEqual(expect.any(Function))
    view.click('A')
    view.rerender(view.props)
    expect(view.registerCanvasPick).toHaveBeenCalledTimes(1)
    act(() => sessionActions.reset())
    expect(view.registerCanvasPick).toHaveBeenLastCalledWith(null)
    await openAndLoad(entities, 'two.dxf')
    expect(view.registerCanvasPick.mock.lastCall[0]).toEqual(expect.any(Function))
    view.unmount()
    expect(view.registerCanvasPick).toHaveBeenLastCalledWith(null)
    expect(view.registerCanvasPick).toHaveBeenCalledTimes(4)
  })

  it('SSD1-24B additive pick selects the first decimal engine id', async () => {
    const view = await setup()
    expect(view.click('A')).toBe(true)
    expect(selectedIds).toEqual(['10'])
  })

  it('SSD1-24B additive second pick survives the null console echo', async () => {
    const view = await setup()
    view.click('A')
    view.rerender({ ...view.props, selectedHandle: 'A' })
    expect(view.click('B')).toBe(true)
    expect(selectedIds).toEqual(['10', '11'])
    expect(view.onSelectedHandleChange).toHaveBeenLastCalledWith(null)
    view.rerender({ ...view.props, selectedHandle: null })
    expect(selectedIds).toEqual(['10', '11'])
    expect(view.registerCanvasPick).toHaveBeenCalledTimes(1)
  })

  it('SSD1-24B additive repeat removes a member and mirrors the remaining handle', async () => {
    const view = await setup()
    view.click('A')
    view.click('B')
    expect(view.click('A')).toBe(true)
    expect(selectedIds).toEqual(['11'])
    expect(view.onSelectedHandleChange).toHaveBeenLastCalledWith('B')
  })

  it('SSD1-24B additive unknown and blank picks are consumed without changing selection', async () => {
    const view = await setup()
    view.click('A')
    expect(view.click('FFFF')).toBe(true)
    expect(selectedIds).toEqual(['10'])
    expect(view.click(null)).toBe(true)
    expect(selectedIds).toEqual(['10'])
  })

  it('SSD1-24B plain blank clears a two-object set', async () => {
    const view = await setup()
    view.click('A')
    view.click('B')
    expect(view.click(null, false)).toBe(true)
    expect(selectedIds).toEqual([])
  })

  it('SSD1-24B plain blank with one object leaves the scalar flow to App', async () => {
    const view = await setup()
    view.click('A')
    expect(view.click(null, false)).toBe(false)
    expect(selectedIds).toEqual(['10'])
    expect(view.click('B', false)).toBe(false)
    expect(selectedIds).toEqual(['10'])
  })
})

describe('SSD1-24C canvas marquee', () => {
  const entities = [{ ...LINE, id: '10' }, { ...CIRCLE, id: '11' }]
  async function setup() {
    const registerCanvasMarquee = vi.fn()
    const view = mount({ registerCanvasMarquee, onSelectedHandleChange: vi.fn() })
    expect(registerCanvasMarquee).not.toHaveBeenCalled()
    await openAndLoad(entities)
    const select = (handles, additive = false) => act(() => registerCanvasMarquee.mock.lastCall[0](handles, { additive }))
    return { ...view, registerCanvasMarquee, select }
  }
  it('SSD1-24C registers only while shown and unregisters when hidden and unmounted', async () => {
    const view = await setup()
    expect(view.registerCanvasMarquee).toHaveBeenCalledTimes(1)
    act(() => sessionActions.reset())
    expect(view.registerCanvasMarquee).toHaveBeenLastCalledWith(null)
    await openAndLoad(entities, 'two.dxf')
    expect(view.registerCanvasMarquee.mock.lastCall[0]).toEqual(expect.any(Function))
    view.unmount()
    expect(view.registerCanvasMarquee).toHaveBeenLastCalledWith(null)
  })
  it('SSD1-24C non-additive marquee replaces the selection', async () => {
    const view = await setup()
    view.select(['A'])
    view.select(['B'])
    expect(selectedIds).toEqual(['11'])
  })
  it('SSD1-24C additive marquee unions without duplicates using current selection', async () => {
    const view = await setup()
    view.select(['A'])
    view.select(['B', 'A', 'B'], true)
    expect(selectedIds).toEqual(['10', '11'])
  })
  it('SSD1-24C unknown handles are ignored', async () => {
    const view = await setup()
    view.select(['FFFF', 'B'])
    expect(selectedIds).toEqual(['11'])
  })
  it('SSD1-24C empty non-additive marquee clears', async () => {
    const view = await setup()
    view.select(['A', 'B'])
    view.select([])
    expect(selectedIds).toEqual([])
  })
  it('SSD1-24C empty additive marquee leaves selection unchanged', async () => {
    const view = await setup()
    view.select(['A'])
    const before = selectedIds
    const replace = vi.spyOn(sessionActions, 'selectReplace')
    view.select([], true)
    expect(selectedIds).toBe(before)
    expect(replace).not.toHaveBeenCalled()
  })
})

describe('EngineDocumentView (W4f slice A0)', () => {
  it('row21 reapplies the retained dirty starter after the Viewer seats console intake', async () => {
    const session = { engineParsed: true, documentId: 'solar-starter.dxf', dirty: true, entities: [LINE], undoDepth: 1, redoDepth: 0, selectedId: '', actions: { select: vi.fn() } }
    vi.spyOn(engineSessionContext, 'useEngineSessionContext').mockImplementation(() => ({ session, highlightedIds: [] }))
    let canvasIntake = null
    const viewer = { applyVersion: vi.fn((intake) => { canvasIntake = intake }) }
    const viewerRef = { current: viewer }
    const onShown = vi.fn()
    function ViewerIntakeReset({ intake }) {
      useEffect(() => { if (intake) canvasIntake = intake }, [intake])
      return null
    }
    const tree = (consoleIntake) => <>
      <EngineDocumentView viewerRef={viewerRef} onShown={onShown} consoleIntake={consoleIntake} />
      <ViewerIntakeReset intake={consoleIntake} />
    </>
    const view = render(tree(null))
    const projection = canvasIntake
    expect(projection.documentId).toBe('solar-starter.dxf')
    expect(projection.polylines).toHaveLength(1)
    const consoleIntake = { dwg: 'real.dxf', polylines: [] }
    await act(async () => {
      view.rerender(tree(consoleIntake))
    })
    // The Viewer's passive effect ran after the bridge in this same commit.
    expect(canvasIntake).toBe(projection)
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
    expect(session.documentId).toBe('solar-starter.dxf')
    expect(session.dirty).toBe(true)
  })

  it('compares mixed numeric and string ids without reporting an existing entity as created', async () => {
    mount()
    await openAndLoad([{ ...LINE, id: 1 }])
    act(() => {
      workers[0].listeners.get('message')({ data: { type: 'editApplied', op: 'createLine', ok: true, createdId: '42', entities: [{ ...LINE, id: '1' }, { ...LINE, id: '42' }], entityCount: 2, bytes: new Uint8Array([49]), byteLength: 1 } })
      sessionActions.select('1')
    })
    expect(onShown.mock.lastCall[1].createdResult).toEqual({ documentId: 'one.dxf', handle: '2A', kind: 'LINE' })
  })

  it('reports a pending creation when its status arrives after the entities and depth', () => {
    const session = { engineParsed: true, documentId: 'one.dxf', entities: [LINE], undoDepth: 0, redoDepth: 0, selectedId: 'e1', status: 'Ready', actions: { select: vi.fn() } }
    vi.spyOn(engineSessionContext, 'useEngineSessionContext').mockImplementation(() => ({ session, highlightedIds: [] }))
    const viewer = { applyVersion: vi.fn() }
    const viewerRef = { current: viewer }
    const onShown = vi.fn()
    const tree = () => <EngineDocumentView viewerRef={viewerRef} onShown={onShown} />
    const utils = render(tree())
    session.entities = [LINE, { ...LINE, id: '42' }]
    session.undoDepth = 1
    utils.rerender(tree())
    expect(onShown.mock.lastCall[1].createdResult).toBeNull()
    const applies = viewer.applyVersion.mock.calls.length
    session.status = 'LINE applied: entity 42'
    utils.rerender(tree())
    expect(onShown.mock.lastCall[1].createdResult).toEqual({ documentId: 'one.dxf', handle: '2A', kind: 'LINE' })
    expect(viewer.applyVersion).toHaveBeenCalledTimes(applies)
    onShown.mockClear()
    utils.rerender(tree())
    expect(onShown).not.toHaveBeenCalled()
  })

  it('reports document close and unmount through onHidden exactly once', async () => {
    const onHidden = vi.fn()
    const utils = mount({ onHidden })
    await openAndLoad([LINE])
    act(() => sessionActions.reset())
    expect(onHidden).toHaveBeenCalledTimes(1)
    expect(onShown).toHaveBeenLastCalledWith(null)
    await openAndLoad([CIRCLE], 'two.dxf')
    utils.unmount()
    expect(onHidden).toHaveBeenCalledTimes(2)
  })

  it('publishes create identity and engine undo/redo depths without refitting on selection', async () => {
    mount()
    await openAndLoad([LINE])
    const created = { ...LINE, id: '42', layer: 'New layer', vertices: [[10, 0, 0], [10, 10, 0]] }
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, createdId: '42', entities: [LINE, created], entityCount: 2, bytes: new Uint8Array([49]), byteLength: 1 })
    const [intake, history] = onShown.mock.lastCall
    expect(intake.polylines.find((p) => p.handle === '2A')).toMatchObject({ layer: 'New layer', pts: created.vertices })
    expect(history).toEqual({ undoDepth: 1, redoDepth: 0, createdResult: { documentId: 'one.dxf', handle: '2A', kind: 'LINE' } })
    const applies = viewer.applyVersion.mock.calls.length
    act(() => sessionActions.select('e1'))
    expect(viewer.applyVersion).toHaveBeenCalledTimes(applies)
    act(() => sessionActions.undo())
    workers[0].emit({ type: 'documentLoaded', documentId: 'one.dxf', entities: [LINE], entityCount: 1, unsupported: [] })
    expect(onShown.mock.lastCall[0].polylines).toHaveLength(1)
    expect(onShown.mock.lastCall[1]).toEqual({ undoDepth: 0, redoDepth: 1, createdResult: null })
    act(() => sessionActions.redo())
    workers[0].emit({ type: 'documentLoaded', documentId: 'one.dxf', entities: [LINE, created], entityCount: 2, unsupported: [] })
    expect(onShown.mock.lastCall[0].polylines).toHaveLength(2)
    expect(onShown.mock.lastCall[1]).toEqual({ undoDepth: 1, redoDepth: 0, createdResult: null })
  })

  it('reports the created entity when the same batch selects an existing entity', async () => {
    mount()
    await openAndLoad([LINE])
    const created = { ...LINE, id: '42' }
    act(() => {
      workers[0].listeners.get('message')({ data: { type: 'editApplied', op: 'createLine', ok: true, createdId: '42', entities: [LINE, created], entityCount: 2, bytes: new Uint8Array([49]), byteLength: 1 } })
      sessionActions.select('e1')
    })
    expect(screen.getByTestId('engine-selection').textContent).toBe('e1')
    expect(onShown.mock.lastCall[1].createdResult).toEqual({ documentId: 'one.dxf', handle: '2A', kind: 'LINE' })
  })

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
    expect(intake.layers).toEqual(['Panels', '0'])
    expect(onShown).toHaveBeenCalledWith(intake, { undoDepth: 0, redoDepth: 0, createdResult: null })
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
