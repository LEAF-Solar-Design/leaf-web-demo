// W4f slice A1: a click on the ground answers the armed prompt's point
// steps through the viewer's unproject, the caret moves on, the rubber band
// follows the cursor, and nothing happens without an armed point command.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'

import CadEditSurface from './CadEditSurface.jsx'
import CanvasPointPicker from './CanvasPointPicker.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0, 0], [100, 50, 0]] }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
let viewer
let ground
let onPicking
let context
function Probe() { context = useEngineSessionContext(); return null }
function mount() {
  workers = []
  // World = client / 10, so a click at (120, 30) is world (12, 3).
  viewer = { unproject: vi.fn((cx, cy) => ({ x: cx / 10, y: cy / 10 })), setRubberBand: vi.fn(), setSnapMarker: vi.fn() }
  ground = document.createElement('div')
  document.body.appendChild(ground)
  onPicking = vi.fn()
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
      </DraftingRibbon>
      <CanvasPointPicker viewerRef={{ current: viewer }} ground={ground} onPicking={onPicking} />
      <CadEditSurface enabled />
    </EngineSessionProvider>,
  )
}

async function openAndLoad(entities = [LINE]) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf()] } })
    await Promise.resolve()
    await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBeGreaterThan(0))
  workers[0].emit({ type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [] })
}

function click(x, y) {
  act(() => {
    ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: x, clientY: y, button: 0, bubbles: true }))
    ground.dispatchEvent(new MouseEvent('pointerup', { clientX: x, clientY: y, button: 0, bubbles: true }))
  })
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
  // Synchronous frames: the callback runs at once and no handle stays
  // pending (a real browser sets the handle before the callback runs, so
  // the picker's "one frame in flight" latch clears on every draw).
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(); return 0 })
})
afterEach(() => { cleanup(); ground?.remove(); vi.restoreAllMocks() })

describe('CanvasPointPicker (W4f slice A1)', () => {
  it('nothing is picked, stamped or ghosted without an armed point command', async () => {
    mount()
    await openAndLoad()
    click(120, 30)
    expect(viewer.unproject).not.toHaveBeenCalled()
    expect(onPicking).not.toHaveBeenCalledWith(true)
    expect(screen.queryByTestId('cockpit-prompt')).toBeNull()
  })

  it('two clicks answer LINE\'s two point steps, move the caret, draw the rubber band, and stand the console selection aside', async () => {
    mount()
    await openAndLoad()
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="draw:createLine"]'))
    expect(onPicking).toHaveBeenLastCalledWith(true)
    click(120, 30)
    expect(screen.getByLabelText('ribbon x').value).toBe('12')
    expect(screen.getByLabelText('ribbon y').value).toBe('3')
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
    // The band follows the cursor from the first point.
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 200, clientY: 80, bubbles: true })) })
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[12, 3], [20, 8]], false)
    click(200, 80)
    expect(screen.getByLabelText('ribbon x2').value).toBe('20')
    expect(screen.getByLabelText('ribbon y2').value).toBe('8')
    expect(document.activeElement).toBe(screen.getByTestId('cockpit-prompt-run'))
    // A third click changes nothing: the sequence is complete.
    click(50, 50)
    expect(screen.getByLabelText('ribbon x').value).toBe('12')
    // Enter runs with the picked operands.
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Enter' })
    const posted = workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'createLine', payload: { x1: 12, y1: 3, x2: 20, y2: 8, layer: '' } })
    // Cancelling clears the band and the stamp.
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Escape' })
    expect(onPicking).toHaveBeenLastCalledWith(false)
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith(null)
  })

  it('a chained LINE (armed with a chain point) starts at the next-point step: the band runs from it and one click finishes (W4f-3)', async () => {
    mount()
    await openAndLoad()
    act(() => { context.setArmed({ group: 'draw', op: 'createLine', from: [12, 3] }) })
    expect(onPicking).toHaveBeenLastCalledWith(true)
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 200, clientY: 80, bubbles: true })) })
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[12, 3], [20, 8]], false)
    click(200, 80)
    expect(screen.getByLabelText('ribbon x2').value).toBe('20')
    expect(screen.getByLabelText('ribbon y2').value).toBe('8')
    expect(document.activeElement).toBe(screen.getByTestId('cockpit-prompt-run'))
    // The chain point itself was never written: the fields' first point is
    // the ribbon's business (it set them when it chained).
    expect(screen.getByLabelText('ribbon x').value).not.toBe('12')
  })

  it('F8 toggles ORTHO; with it on the second point and the band snap to the axis of the larger move (W4f-4)', async () => {
    mount()
    await openAndLoad()
    expect(context.ortho).toBe(false)
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F8', bubbles: true, cancelable: true })) })
    expect(context.ortho).toBe(true)
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="draw:createLine"]'))
    // The first point is free.
    click(120, 30)
    expect(screen.getByLabelText('ribbon x').value).toBe('12')
    expect(screen.getByLabelText('ribbon y').value).toBe('3')
    // The band and the pick hold y (the larger move is horizontal).
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 200, clientY: 50, bubbles: true })) })
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[12, 3], [20, 3]], false)
    click(200, 50)
    expect(screen.getByLabelText('ribbon x2').value).toBe('20')
    expect(screen.getByLabelText('ribbon y2').value).toBe('3')
    // Off again: a free pick.
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F8', bubbles: true, cancelable: true })) })
    expect(context.ortho).toBe(false)
    act(() => { context.setArmed({ group: 'draw', op: 'createLine', from: [12, 3] }) })
    click(200, 50)
    expect(screen.getByLabelText('ribbon y2').value).toBe('5')
  })

  it('OSNAP is on from the start (W4f-7); F3 toggles it; on, a pick within reach of an endpoint lands on it, the marker follows, and the snap beats ORTHO (W4f-5)', async () => {
    mount()
    await openAndLoad()
    // The reach: SNAP_PX 10 px = 1 world unit under the mock projection. The
    // document's LINE ends at (100, 50); a click at world (100.3, 49.7) is
    // within reach with OSNAP on and a raw pick with it off. On is the
    // default (W4f-7), so F3 first turns it off for the raw pick.
    expect(context.osnap).toBe(true)
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F3', bubbles: true, cancelable: true })) })
    expect(context.osnap).toBe(false)
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="draw:createLine"]'))
    click(1003, 497)
    expect(screen.getByLabelText('ribbon x').value).toBe('100.3')
    expect(screen.getByLabelText('ribbon y').value).toBe('49.7')
    expect(viewer.setSnapMarker).not.toHaveBeenCalled()
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F3', bubbles: true, cancelable: true })) })
    expect(context.osnap).toBe(true)
    act(() => { context.setArmed({ group: 'draw', op: 'createLine', from: [0, 0] }) })
    // Hovering near the endpoint shows the marker at it (once per change)
    // and the band runs to the snapped point; hovering away clears it.
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 1003, clientY: 497, bubbles: true })) })
    expect(viewer.setSnapMarker).toHaveBeenLastCalledWith({ x: 100, y: 50 }, 1)
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[0, 0], [100, 50]], false)
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 1004, clientY: 496, bubbles: true })) })
    expect(viewer.setSnapMarker).toHaveBeenCalledTimes(1)
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 700, clientY: 700, bubbles: true })) })
    expect(viewer.setSnapMarker).toHaveBeenLastCalledWith(null)
    // With ORTHO on as well, the snap wins.
    act(() => { context.setOrtho(true) })
    click(1003, 497)
    expect(screen.getByLabelText('ribbon x2').value).toBe('100')
    expect(screen.getByLabelText('ribbon y2').value).toBe('50')
    // Off again (F3): the same click is a raw (here ORTHO-held) pick.
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F3', bubbles: true, cancelable: true })) })
    expect(context.osnap).toBe(false)
    act(() => { context.setOrtho(false) })
    // (the same chain point keeps the same armed object, so disarm first to
    // start a fresh sequence)
    act(() => { context.setArmed(null) })
    act(() => { context.setArmed({ group: 'draw', op: 'createLine', from: [0, 0] }) })
    click(1003, 497)
    expect(screen.getByLabelText('ribbon x2').value).toBe('100.3')
  })

  it('a circle takes its centre and a radius point; a drag (pointer travel) is never a pick', async () => {
    mount()
    await openAndLoad()
    act(() => { context.setArmed({ group: 'draw', op: 'createCircle' }) })
    act(() => {
      ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: 0, clientY: 0, button: 0, bubbles: true }))
      ground.dispatchEvent(new MouseEvent('pointerup', { clientX: 40, clientY: 0, button: 0, bubbles: true }))
    })
    expect(screen.getByLabelText('ribbon x').value).toBe('0')
    click(100, 100)
    expect(screen.getByLabelText('ribbon x').value).toBe('10')
    expect(screen.getByLabelText('ribbon y').value).toBe('10')
    click(130, 140)
    expect(screen.getByLabelText('ribbon r').value).toBe('5')
    expect(document.activeElement).toBe(screen.getByTestId('cockpit-prompt-run'))
  })
})

// W4g-6: the EDGE pick. A click resolves to the nearest entity OTHER than the
// selection within the aperture (one extra unproject SNAP_PX to the right),
// writes its id and the click point, and moves the caret on to the point step.
describe('W4g-6 edge picks', () => {
  const H = { id: '7', type: 'LINE', layer: 'A', closed: false, editable: true, vertices: [[0, 0, 0], [10, 0, 0]], radius: null, startDeg: null, endDeg: null }
  const V = { id: '9', type: 'LINE', layer: 'B', closed: false, editable: true, vertices: [[5, -5, 0], [5, 5, 0]], radius: null, startDeg: null, endDeg: null }

  it('a TRIM click on the other line fills the edge field and its point; a click on nothing waits; the next click is the plain point', async () => {
    mount()
    await openAndLoad([H, V])
    act(() => { context.session.actions.select('7') })
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="modify:trim"]'))
    await waitFor(() => expect(screen.getByTestId('cockpit-prompt').getAttribute('data-op')).toBe('trim'))
    // World = client / 10: (52, 30) is (5.2, 3), 0.2 from the vertical and
    // 3 from the horizontal (the selection, never a candidate). The aperture
    // is 10 px = 1 world unit here.
    click(300, 300)
    expect(screen.getByLabelText('ribbon edge', { exact: true }).value).toBe('')
    click(52, 30)
    expect(screen.getByLabelText('ribbon edge', { exact: true }).value).toBe('9')
    // TRIM shows no edge-point fields (FILLET and CHAMFER do), but the click
    // point rides the record all the same, read by the planner.
    expect(context.inputs.ex).toBe('5.2')
    expect(context.inputs.ey).toBe('3')
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x', { exact: true }))
    click(80, 5)
    expect(screen.getByLabelText('ribbon x', { exact: true }).value).toBe('8')
    expect(screen.getByLabelText('ribbon y', { exact: true }).value).toBe('0.5')
    expect(screen.getByRole('button', { name: 'Run' }).disabled).toBe(false)
  })

  it('W4g-6d: a FILLET edge click on the selected POLYLINE names it (its own corner); on a selected LINE it still waits', async () => {
    // The square sits away from H so a click on either names one entity only.
    const SQ = { id: '13', type: 'LWPOLYLINE', layer: 'A', closed: true, editable: true, vertices: [[20, 20, 0], [30, 20, 0], [30, 30, 0], [20, 30, 0]], bulges: [0, 0, 0, 0], radius: null, startDeg: null, endDeg: null }
    mount()
    await openAndLoad([H, SQ])
    act(() => { context.session.actions.select('13') })
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="modify:fillet"]'))
    await waitFor(() => expect(screen.getByTestId('cockpit-prompt').getAttribute('data-op')).toBe('fillet'))
    // The radius is the prompt's first step; the edge fields follow it.
    fireEvent.change(screen.getByLabelText('ribbon radius', { exact: true }), { target: { value: '2' } })
    await waitFor(() => expect(screen.getByLabelText('ribbon edge', { exact: true }).value).toBe(''))
    // (250, 300) is world (25, 30): on the square's top side, 20 from every other entity.
    click(250, 300)
    expect(screen.getByLabelText('ribbon edge', { exact: true }).value).toBe('13')
    expect(context.inputs.ex).toBe('25')
    expect(context.inputs.ey).toBe('30')
    // The point step then lands on the square's right side: the two picks name the corner (30,30).
    click(300, 250)
    expect(screen.getByLabelText('ribbon x', { exact: true }).value).toBe('30')
    expect(screen.getByLabelText('ribbon y', { exact: true }).value).toBe('25')
    // A LINE is never its own second object: the same click on the selection waits.
    // (A second click on the armed tool cancels it; the third arms it again.)
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="modify:fillet"]'))
    await waitFor(() => expect(screen.queryByTestId('cockpit-prompt')).toBeNull())
    act(() => { context.session.actions.select('7') })
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="modify:fillet"]'))
    await waitFor(() => expect(screen.getByTestId('cockpit-prompt').getAttribute('data-op')).toBe('fillet'))
    fireEvent.change(screen.getByLabelText('ribbon radius', { exact: true }), { target: { value: '2' } })
    // The edge field keeps its last value across a re-arm; clear it so the click's silence is visible.
    fireEvent.change(screen.getByLabelText('ribbon edge', { exact: true }), { target: { value: '' } })
    await waitFor(() => expect(screen.getByLabelText('ribbon edge', { exact: true }).value).toBe(''))
    click(20, 0)
    expect(screen.getByLabelText('ribbon edge', { exact: true }).value).toBe('')
  })
})
