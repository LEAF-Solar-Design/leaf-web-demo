// W4f slice A1: a click on the ground answers the armed prompt's point
// steps through the viewer's unproject, the caret moves on, the rubber band
// follows the cursor, and nothing happens without an armed point command.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'

import CadEditSurface from './CadEditSurface.jsx'
import CanvasPointPicker from './CanvasPointPicker.jsx'
import CommandLineArmer from './CommandLineArmer.jsx'
import { applyPick, startPicking } from './pointPicking.js'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { SNAP_MODES } from './snapModes.js'
import StatusModesBridge from './StatusModesBridge.jsx'

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
let probeRenders = 0
function Probe() { context = useEngineSessionContext(); probeRenders += 1; return null }
function mount(withArmer = false, canvasSelector = null, extra = null, viewerMethods = null) {
  workers = []
  // World = client / 10, so a click at (120, 30) is world (12, 3).
  viewer = { unproject: vi.fn((cx, cy) => ({ x: cx / 10, y: cy / 10 })), setRubberBand: vi.fn(), setSnapMarker: vi.fn(), ...(viewerMethods || {}) }
  ground = document.createElement('div')
  document.body.appendChild(ground)
  onPicking = vi.fn()
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      {withArmer && <CommandLineArmer />}
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
      </DraftingRibbon>
      <CanvasPointPicker viewerRef={{ current: viewer }} ground={ground} onPicking={onPicking} canvasSelector={canvasSelector} />
      <CadEditSurface enabled />
      {extra}
    </EngineSessionProvider>,
  )
}

async function openAndLoad(entities = [LINE], blocks = undefined) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf()] } })
    await Promise.resolve()
    await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBeGreaterThan(0))
  workers[0].emit({
    type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [],
    ...(blocks ? { blocks } : {}),
  })
}

function click(x, y) {
  act(() => {
    ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: x, clientY: y, button: 0, bubbles: true }))
    ground.dispatchEvent(new MouseEvent('pointerup', { clientX: x, clientY: y, button: 0, bubbles: true }))
  })
}

it('a bar point uses the click sequence and its ghost anchor', async () => {
  mount()
  await openAndLoad()
  act(() => { context.setArmed({ group: 'draw', op: 'createLine' }); context.setOsnap(false) })
  const detail = { point: [5, 5], key: 'x', handled: false }
  act(() => window.dispatchEvent(new CustomEvent('cockpit:pick-point', { detail })))
  expect(detail.handled).toBe(true)
  act(() => ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 100, clientY: 0 })))
  expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[5, 5], [10, 0]], false)
  click(100, 0)
  expect(context.inputs).toMatchObject({ x: '5', y: '5', x2: '10', y2: '0' })
})

it('GROUP appends picked edges and ignores the selection and duplicate picks', async () => {
  mount()
  const lines = [0, 10, 20].map((y, i) => ({ id: String(10 + i), type: 'LINE', editable: true, vertices: [[0, y, 0], [3, y, 0]] }))
  await openAndLoad(lines)
  act(() => { context.session.actions.select('10'); context.setArmed({ group: 'groups', op: 'group' }) })
  click(15, 0); click(15, 100); click(15, 100); click(15, 200)
  expect(context.inputs.members).toBe('11 12')
  expect(context.session.selectedId).toBe('10')
  expect(workers[0].posted.filter((message) => message.type === 'applyEdit')).toEqual([])
})

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
  // Synchronous frames: the callback runs at once and no handle stays
  // pending (a real browser sets the handle before the callback runs, so
  // the picker's "one frame in flight" latch clears on every draw).
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(); return 0 })
})

it('BLOCK resolves before exclusion, toggles members, then Enter advances to the base pick', async () => {
  mount()
  const lines = [0, 0.2, 10].map((y, i) => ({ id: String(16 + i), type: 'LINE', editable: true, vertices: [[0, y, 0], [3, y, 0]] }))
  await openAndLoad(lines)
  act(() => { context.session.actions.select('16'); context.setArmed({ group: 'draw', op: 'createBlock' }) })
  click(15, 0)
  expect(context.inputs.members).toBe('')
  click(15, 100)
  expect(context.inputs.members).toBe('18')
  click(15, 100)
  expect(context.inputs.members).toBe('')
  click(15, 100)
  expect([...context.highlightedIds]).toEqual(['16', '18'])
  fireEvent.keyDown(screen.getByLabelText('ribbon members'), { key: 'Enter' })
  expect(context.inputs.membersDone).toBe('true')
  click(100, 200)
  expect(context.inputs.x).toBe('10')
  expect(context.inputs.y).toBe('20')
  expect(screen.getByLabelText('ribbon block name').hasAttribute('list')).toBe(false)
})
afterEach(() => { cleanup(); ground?.remove(); vi.restoreAllMocks() })

it('BLOCK consumes a circle rim click before the viewer can replace its selected LINE', async () => {
  mount()
  const line = { id: '16', type: 'LINE', editable: true, vertices: [[12, 23, 0], [17, 23, 0]] }
  const circle = { id: '17', type: 'CIRCLE', editable: true, vertices: [[11, 24, 0]], radius: 2 }
  await openAndLoad([line, circle])
  act(() => { context.session.actions.select('16') })
  act(() => { context.setArmed({ group: 'draw', op: 'createBlock' }) })
  expect(onPicking).toHaveBeenLastCalledWith(true)
  expect(screen.getByLabelText('ribbon members').textContent).toBe('1 objects')
  // The native viewer listener is on a child canvas, before the ground's
  // bubbling listener. A member click must never reach that selection path.
  const canvas = document.createElement('canvas')
  ground.appendChild(canvas)
  const select = vi.fn(() => context.session.actions.select('17'))
  canvas.addEventListener('pointerup', select)
  act(() => {
    canvas.dispatchEvent(new MouseEvent('pointerdown', { clientX: 90, clientY: 240, button: 0, bubbles: true }))
    canvas.dispatchEvent(new MouseEvent('pointerup', { clientX: 90, clientY: 240, button: 0, bubbles: true }))
  })
  expect(select).not.toHaveBeenCalled()
  expect(context.inputs.members).toBe('17')
  expect(screen.getByLabelText('ribbon members').textContent).toBe('2 objects')
  expect(context.session.selectedId).toBe('16')
  expect(context.inputs.membersDone).toBe('')
})

it('a completed LINE run leaves the caret in the empty chained x2 field', async () => {
  mount()
  await openAndLoad()
  act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
  click(120, 30)
  click(200, 80)
  const end = { x: context.inputs.x2, y: context.inputs.y2 }
  fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Enter' })
  expect(context.session.busy).toBe(true)
  act(() => workers[0].emit({
    type: 'editApplied', op: 'createLine', ok: true, createdId: 'e2',
    entities: [LINE, { ...LINE, id: 'e2', vertices: [[12, 3, 0], [20, 8, 0]] }],
    entityCount: 2, unsupported: [], bytes: new Uint8Array([1, 2, 3]),
  }))
  await waitFor(() => expect(context.inputs.x).toBe(end.x))
  expect(context.inputs.y).toBe(end.y)
  expect(context.inputs.x2).toBe('')
  expect(context.inputs.y2).toBe('')
  expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
})

it('resolves the nearest edge before excluding picked or selected members', async () => {
  const lines = [0, 0.2].map((y, i) => ({ id: String(10 + i), type: 'LINE', editable: true, vertices: [[0, y, 0], [3, y, 0]] }))
  const state = startPicking('group')
  const edge = { entities: lines, tol: 1, exceptId: null }
  expect(applyPick(state, 1.5, 0, {}, edge).writes).toEqual([['members', '10']])
  expect(applyPick(state, 1.5, 0, { members: '10' }, edge).writes).toEqual([])
  expect(applyPick(state, 1.5, 0, {}, { ...edge, exceptId: '10' }).writes).toEqual([])
  mount()
  await openAndLoad(lines)
  act(() => context.setArmed({ group: 'groups', op: 'group' }))
  click(15, 0)
  expect(context.inputs.members).toBe('10')
  click(15, 0)
  expect(context.inputs.members).toBe('10')
  act(() => { context.setInput('members', ''); context.session.actions.select('10') })
  click(15, 0)
  expect(context.inputs.members).toBe('')
})

describe('canvas ownership', () => {
  async function setup() {
    mount(false, '.viewer-canvas')
    ground.innerHTML = '<div class="viewer-canvas"><canvas></canvas></div><div class="studio-ground-board"></div>'
    await openAndLoad()
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }); context.setOsnap(false) })
    return { canvas: ground.querySelector('canvas'), board: ground.querySelector('.studio-ground-board') }
  }
  const pointer = (target, type) => act(() => {
    target.dispatchEvent(new MouseEvent(type, { clientX: 120, clientY: 30, button: 0, bubbles: true }))
  })
  const expectNoPick = (initialInputs) => {
    expect(viewer.unproject).not.toHaveBeenCalled()
    expect(context.inputs.x).toBe(initialInputs.x)
    expect(context.inputs.y).toBe(initialInputs.y)
  }

  it('does not complete a board press on the canvas', async () => {
    const { canvas, board } = await setup()
    const initialInputs = { x: context.inputs.x, y: context.inputs.y }
    pointer(board, 'pointerdown')
    pointer(canvas, 'pointerup')
    expectNoPick(initialInputs)
  })

  it('forgets a canvas press released over chrome', async () => {
    const { canvas } = await setup()
    const initialInputs = { x: context.inputs.x, y: context.inputs.y }
    pointer(canvas, 'pointerdown')
    pointer(document.body, 'pointerup')
    pointer(canvas, 'pointerup')
    expectNoPick(initialInputs)
  })

  it('forgets a canvas press canceled on window', async () => {
    const { canvas } = await setup()
    const initialInputs = { x: context.inputs.x, y: context.inputs.y }
    pointer(canvas, 'pointerdown')
    pointer(window, 'pointercancel')
    pointer(canvas, 'pointerup')
    expectNoPick(initialInputs)
  })

  it('records a complete canvas click', async () => {
    const { canvas } = await setup()
    pointer(canvas, 'pointerdown')
    pointer(canvas, 'pointerup')
    expect(viewer.unproject).toHaveBeenCalledTimes(1)
    expect(viewer.unproject).toHaveBeenCalledWith(120, 30)
    expect(context.inputs).toMatchObject({ x: '12', y: '3' })
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
  })
})

describe('CanvasPointPicker (W4f slice A1)', () => {
  it('focuses Run after the second LINE canvas pick with the armer live', async () => {
    mount(true)
    await openAndLoad()
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }); context.setOsnap(false) })
    click(120, 30)
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
    click(200, 80)
    expect(context.inputs).toMatchObject({ x: '12', y: '3', x2: '20', y2: '8' })
    expect(document.activeElement).toBe(screen.getByTestId('cockpit-prompt-run'))
  })

  it('still claims a focus-step without complete and focuses the armer cursor field', async () => {
    mount(true)
    await openAndLoad()
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }); context.setOsnap(false) })
    click(120, 30)
    act(() => screen.getByLabelText('ribbon y2').focus())
    const detail = { handled: false }
    act(() => window.dispatchEvent(new CustomEvent('cockpit:focus-step', { detail })))
    expect(detail.handled).toBe(true)
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
  })

  it('keeps the LINE caret handoff and next pick across a same-op input update', async () => {
    mount()
    await openAndLoad()
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }, { rearm: true }) })
    click(120, 30)
    const nextField = screen.getByLabelText('ribbon x2')
    expect(document.activeElement).toBe(nextField)
    act(() => {
      context.setInput('x', '13')
      context.setArmed({ group: 'draw', op: 'createLine' })
    })
    expect(context.inputs.x).toBe('13')
    expect(context.inputs.y).toBe('3')
    expect(document.activeElement).toBe(nextField)
    click(200, 80)
    expect(context.inputs.x).toBe('13')
    expect(context.inputs.x2).toBe('20')
    expect(document.activeElement).toBe(screen.getByTestId('cockpit-prompt-run'))
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }, { rearm: true }) })
    click(150, 40)
    expect(context.inputs.x).toBe('15')
    expect(context.inputs.y).toBe('4')
    expect(document.activeElement).toBe(nextField)
  })
  it('MLEADER picks arrowhead and landing with a rubber band, then posts the text once', async () => {
    mount()
    await openAndLoad()
    act(() => context.setArmed({ group: 'draw', op: 'createMleader' }))
    click(300, 230)
    expect(screen.getByLabelText('ribbon x').value).toBe('30')
    expect(screen.getByLabelText('ribbon y').value).toBe('23')
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 350, clientY: 260, bubbles: true })) })
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[30, 23], [35, 26]], false)
    click(350, 260)
    expect(screen.getByLabelText('ribbon x2').value).toBe('35')
    expect(screen.getByLabelText('ribbon y2').value).toBe('26')
    click(500, 500)
    expect(screen.getByLabelText('ribbon x2').value).toBe('35')
    fireEvent.change(screen.getByLabelText('ribbon text'), { target: { value: 'Valve' } })
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toEqual([
      { type: 'applyEdit', op: 'createMleader', payload: { x: 30, y: 23, x2: 35, y2: 26, text: 'Valve', style: 'Standard', layer: '' } },
    ])
  })

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
    expect(viewer.setSnapMarker).toHaveBeenLastCalledWith({ x: 100, y: 50, kind: 'endpoint' }, 1)
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

// W4g-7b: the INSERT ghost reads the typed name, scale and rotation off the
// live inputs and the block definition off the document's own catalogue
// (session.entities.blocks), the wiring CanvasPointPicker adds to the pick
// gesture (pointPicking.js owns the box math itself).
describe('W4g-7b the INSERT ghost', () => {
  const FIXTURE = { name: 'Fixture', base: [1, 2, 0], children: [{ type: 'LINE', vertices: [[1, 2, 0], [4, 2, 0]] }], complete: true, baseUnknown: false, digest: 'd1' }

  it('scales and rotates the named definition about its base to the cursor, reading the typed sx/sy/rot from the live inputs', async () => {
    mount()
    await openAndLoad([LINE], [FIXTURE])
    act(() => { context.setArmed({ group: 'draw', op: 'createInsert' }) })
    act(() => {
      context.setInput('name', 'Fixture')
      context.setInput('sx', '2')
      context.setInput('sy', '3')
      context.setInput('rot', '90')
    })
    // World = client / 10: cursor at (100, 200) is world (10, 20). The
    // fixture's one-segment definition (base (1,2), a line to (4,2)) scaled
    // (2,3) and rotated 90deg about the base, then moved to the cursor, is
    // the chord (10,20)-(10,26): the hand-derived case in the 02c spec.
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 100, clientY: 200, bubbles: true })) })
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[10, 20], [10, 26]], false)
  })

  it('draws no ghost when the typed name names no definition in the catalogue', async () => {
    mount()
    await openAndLoad([LINE], [FIXTURE])
    act(() => { context.setArmed({ group: 'draw', op: 'createInsert' }) })
    act(() => { context.setInput('name', 'Nope') })
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 100, clientY: 200, bubbles: true })) })
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith(null, false)
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
    expect(context.inputs.etol).toBe('')
    // TRIM shows no edge-point fields (FILLET and CHAMFER do), but the click
    // point rides the record all the same, read by the planner.
    expect(context.inputs.ex).toBe('5.2')
    expect(context.inputs.ey).toBe('3')
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x', { exact: true }))
    click(80, 5)
    expect(screen.getByLabelText('ribbon x', { exact: true }).value).toBe('8')
    expect(screen.getByLabelText('ribbon y', { exact: true }).value).toBe('0.5')
    expect(Number(context.inputs.etol)).toBeCloseTo(1, 9)
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

// B1b: the picker drives the merged query with the selected modes and the
// sequence's anchor. Every expectation below is the record's measured table
// (section 4, aperture 1): world = client / 10 here, so a cursor at world
// (9.6, 0.3) is client (96, 3) and the aperture is one world unit.
describe('B1b object snap picking', () => {
  const L = (id, a, b) => ({ id, type: 'LINE', layer: '0', editable: true, vertices: [a, b] })
  const C = { id: 'c1', type: 'CIRCLE', layer: '0', editable: true, vertices: [[0, 0]], radius: 5 }
  const at = (x, y) => [Math.round(x * 10), Math.round(y * 10)]
  const hover = (x, y) => act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: x, clientY: y, bubbles: true })) })
  const onlyModes = (...kinds) => act(() => { for (const { kind } of SNAP_MODES) context.setSnapMode(kind, kinds.includes(kind)) })
  const pressF3 = () => act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F3', bubbles: true, cancelable: true })) })
  const markerCalls = () => viewer.setSnapMarker.mock.calls
  const lastMarker = () => markerCalls().at(-1)
  const expectMarker = (x, y, kind) => {
    const [pt, size] = lastMarker()
    expect(pt.kind).toBe(kind)
    expect(pt.x).toBeCloseTo(x, 12)
    expect(pt.y).toBeCloseTo(y, 12)
    expect(Object.keys(pt).sort()).toEqual(['kind', 'x', 'y'])
    expect(size).toBeCloseTo(1, 9)
  }

  const MEASURED = [
    ['B1B-P01 endpoint', 'endpoint', [L('a', [0, 0], [10, 0])], [9.6, 0.3], null, [10, 0], { x: '10', y: '0' }],
    ['B1B-P02 midpoint', 'midpoint', [L('a', [0, 0], [10, 0])], [5.2, -0.4], null, [5, 0], { x: '5', y: '0' }],
    ['B1B-P03 centre', 'centre', [C], [0.2, 0.1], null, [0, 0], { x: '0', y: '0' }],
    ['B1B-P04 quadrant', 'quadrant', [C], [5.3, -0.2], null, [5, 0], { x: '5', y: '0' }],
    ['B1B-P05 intersection', 'intersection', [L('a', [0, 0], [10, 10]), L('b', [0, 10], [10, 0])], [5.3, 4.8], null, [5, 5], { x: '5', y: '5' }],
    ['B1B-P06 perpendicular', 'perpendicular', [L('a', [2, 0], [2, 10])], [2.2, 5.1], [0, 5], [2, 5], { x2: '2', y2: '5' }],
    ['B1B-P07 tangent', 'tangent', [C], [2.6, 4.4], [10, 0], [2.5, 4.330127018922193], { x2: '2.5', y2: '4.33' }],
    ['B1B-P08 nearest', 'nearest', [L('a', [0, 0], [10, 0])], [0.2, 0.1], null, [0.2, 0], { x: '0.2', y: '0' }],
    ['B1B-P09 insertion', 'insertion', [{ id: 'i1', type: 'INSERT', layer: '0', ip: [3, 4, 0], vertices: [[3, 4, 0]] }], [3.1, 4], null, [3, 4], { x: '3', y: '4' }],
  ]
  for (const [name, kind, entities, cursor, anchor, hit, writes] of MEASURED) {
    it(name, async () => {
      mount()
      await openAndLoad(entities)
      onlyModes(kind)
      expect(context.snapModes).toBe(SNAP_MODES.find((mode) => mode.kind === kind).bit)
      act(() => { context.setArmed(anchor ? { group: 'draw', op: 'createLine', from: anchor } : { group: 'draw', op: 'createLine' }) })
      const [cx, cy] = at(...cursor)
      hover(cx, cy)
      expectMarker(hit[0], hit[1], kind)
      // The ghost runs to the snapped point.
      const band = viewer.setRubberBand.mock.calls.at(-1)[0]
      if (anchor) expect(band[1].map((v, i) => Math.abs(v - hit[i]) < 1e-9)).toEqual([true, true])
      // The click resolves the same coordinate and writes it at three decimals.
      click(cx, cy)
      expect(context.inputs).toMatchObject(writes)
    })
  }

  it('B1B-P10 typed point bypasses snap and ortho', async () => {
    mount()
    await openAndLoad([L('a', [2, 0], [2, 10])])
    onlyModes('perpendicular')
    act(() => { context.setOrtho(true); context.setArmed({ group: 'draw', op: 'createLine', from: [0, 5] }) })
    // Nearby, the pointer would snap to (2, 5)...
    hover(...at(2.2, 5.1))
    expectMarker(2, 5, 'perpendicular')
    // ...but a typed point is accepted exactly as typed (three decimals).
    const detail = { point: [2.2, 5.1], key: 'x2', handled: false }
    act(() => window.dispatchEvent(new CustomEvent('cockpit:pick-point', { detail })))
    expect(detail.handled).toBe(true)
    expect(context.inputs).toMatchObject({ x2: '2.2', y2: '5.1' })
  })

  it('B1B-P11 first pick suppresses only anchored modes', async () => {
    mount()
    await openAndLoad([L('a', [2, 0], [2, 10]), C])
    onlyModes('perpendicular', 'tangent')
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    for (const cursor of [[2.2, 5.1], [2.6, 4.4]]) {
      hover(...at(...cursor))
      expect(viewer.setSnapMarker).not.toHaveBeenCalled()
    }
    // The fixed modes still answer a first pick.
    onlyModes('perpendicular', 'tangent', 'endpoint')
    hover(...at(2.1, 0.2))
    expectMarker(2, 0, 'endpoint')
    // A first click with only anchored modes near is a raw pick.
    onlyModes('perpendicular', 'tangent')
    click(...at(2.2, 5.1))
    expect(context.inputs).toMatchObject({ x: '2.2', y: '5.1' })
    // Once a point is accepted, the same modes answer: the foot from (2.2, 5.1).
    hover(...at(2.3, 5))
    expectMarker(2, 5.1, 'perpendicular')
  })

  it('B1B-P12 accepted anchors', async () => {
    mount()
    await openAndLoad([L('a', [2, 0], [2, 10])])
    onlyModes('perpendicular')
    // LINE continuation: the chain point is the anchor.
    act(() => { context.setArmed({ group: 'draw', op: 'createLine', from: [0, 5] }) })
    hover(...at(2.2, 5.1))
    expectMarker(2, 5, 'perpendicular')
    // Polyline: the last vertex is the anchor.
    act(() => { context.setArmed({ group: 'draw', op: 'createPolyline' }) })
    click(...at(0, 7))
    expect(context.inputs.pts).toBe('0,7')
    hover(...at(2.2, 7.1))
    expectMarker(2, 7, 'perpendicular')
    click(...at(2.2, 7.1))
    expect(context.inputs.pts).toBe('0,7 2,7')
    // MOVE and COPY: the base point is the anchor.
    for (const op of ['move', 'copy']) {
      act(() => { context.setArmed({ group: 'modify', op }) })
      click(...at(0, 3))
      hover(...at(2.2, 3.1))
      expectMarker(2, 3, 'perpendicular')
      click(...at(2.2, 3.1))
      expect(context.inputs).toMatchObject({ dx: '2', dy: '0' })
    }
    // A typed prior point anchors the next pointer query.
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    const detail = { point: [0, 8], key: 'x', handled: false }
    act(() => window.dispatchEvent(new CustomEvent('cockpit:pick-point', { detail })))
    expect(detail.handled).toBe(true)
    hover(...at(2.2, 8.1))
    expectMarker(2, 8, 'perpendicular')
    click(...at(2.2, 8.1))
    expect(context.inputs).toMatchObject({ x: '0', y: '8', x2: '2', y2: '8' })
  })

  it('B1B-P13 stationary mode and F3 redraw', async () => {
    mount()
    await openAndLoad([L('a', [0, 0], [10, 0])])
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    hover(...at(9.6, 0.3))
    expectMarker(10, 0, 'endpoint')
    const calls = markerCalls().length
    // F3 off: the stationary marker clears; the selection is kept.
    pressF3()
    expect(context.osnap).toBe(false)
    expect(lastMarker()).toEqual([null])
    expect(context.snapModes).toBe(23)
    // F3 on: the same cursor shows it again, without a move.
    pressF3()
    expectMarker(10, 0, 'endpoint')
    expect(markerCalls().length).toBe(calls + 2)
    // A mode change re-reads the stationary cursor too.
    act(() => { context.setSnapMode('endpoint', false) })
    expect(lastMarker()).toEqual([null])
    act(() => { context.setSnapMode('endpoint', true) })
    expectMarker(10, 0, 'endpoint')
    act(() => { context.setSnapMode('nearest', true) })
    expectMarker(9.6, 0, 'nearest')
    // The prompt's master button does the same as F3.
    fireEvent.click(screen.getByTestId('cockpit-osnap'))
    expect(context.osnap).toBe(false)
    expect(lastMarker()).toEqual([null])
    fireEvent.click(screen.getByTestId('cockpit-osnap'))
    expectMarker(9.6, 0, 'nearest')
    expect(context.snapModes).toBe(23 | 512)
  })

  it('B1B-P19 a mode switch redraws the marker before the next frame', async () => {
    mount()
    await openAndLoad([L('a', [0, 0], [10, 0])])
    onlyModes('endpoint')
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    hover(...at(9.6, 0.3))
    expectMarker(10, 0, 'endpoint')
    // Frames are held from here: a callback queued by requestAnimationFrame never runs,
    // as when a click lands before the browser's next frame.
    const held = []
    window.requestAnimationFrame.mockImplementation((cb) => { held.push(cb); return held.length })
    // Endpoint swapped for Nearest under a still cursor: the marker follows at once...
    onlyModes('nearest')
    expectMarker(9.6, 0, 'nearest')
    // ...so a click before any frame resolves the point the marker shows.
    click(...at(9.6, 0.3))
    expect(context.inputs).toMatchObject({ x: '9.6', y: '0' })
    // The master switch clears the marker without waiting for a frame too.
    hover(...at(9.6, 0.3))
    pressF3()
    expect(context.osnap).toBe(false)
    expect(lastMarker()).toEqual([null])
  })

  // Astra round two (B1b): every change the next click reads redraws a still
  // cursor's feedback at once, not in a frame a click can beat.
  const holdFrames = () => {
    const held = []
    window.requestAnimationFrame.mockImplementation((cb) => { held.push(cb); return held.length })
    return held
  }

  it('B1B-P20 an anchor change redraws the marker before the next frame', async () => {
    mount()
    await openAndLoad([L('a', [2, 0], [2, 10])])
    onlyModes('perpendicular')
    act(() => { context.setArmed({ group: 'draw', op: 'createLine', from: [0, 5] }) })
    hover(...at(2.2, 5.1))
    expectMarker(2, 5, 'perpendicular')
    const held = holdFrames()
    // The chain point moves under a still cursor: the marker follows at once...
    act(() => { context.setArmed({ group: 'draw', op: 'createLine', from: [0, 5.5] }) })
    expect(held).toEqual([])
    expectMarker(2, 5.5, 'perpendicular')
    // ...so a click before any frame resolves the point the marker shows.
    click(...at(2.2, 5.1))
    expect(context.inputs).toMatchObject({ x2: '2', y2: '5.5' })
  })

  it('B1B-P21 an entity change redraws the marker before the next frame', async () => {
    mount()
    await openAndLoad([L('a', [0, 0], [10, 0])])
    onlyModes('endpoint')
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    hover(...at(9.6, 0.3))
    expectMarker(10, 0, 'endpoint')
    const held = holdFrames()
    // An applied edit moves the line half a unit up under a still cursor; the
    // command stays armed and the marker follows at once...
    act(() => workers[0].emit({
      type: 'editApplied', op: 'translateEntity', ok: true,
      entities: [L('a', [0, 0.5], [10, 0.5])], entityCount: 1, unsupported: [], bytes: new Uint8Array([1, 2, 3]),
    }))
    expect(context.armed?.op).toBe('createLine')
    expect(held).toEqual([])
    expectMarker(10, 0.5, 'endpoint')
    // ...so a click before any frame resolves the point the marker shows.
    click(...at(9.6, 0.3))
    expect(context.inputs).toMatchObject({ x: '10', y: '0.5' })
  })

  it('B1B-P22 F8 redraws the still ghost at once with OSNAP off', async () => {
    mount()
    await openAndLoad([L('a', [20, 20], [30, 20])])
    act(() => { context.setOsnap(false); context.setArmed({ group: 'draw', op: 'createLine', from: [0, 0] }) })
    hover(60, 10)
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[0, 0], [6, 1]], false)
    const held = holdFrames()
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F8', bubbles: true, cancelable: true })) })
    expect(context.ortho).toBe(true)
    expect(held).toEqual([])
    // The ghost already runs to the point the click writes.
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[0, 0], [6, 0]], false)
    click(60, 10)
    expect(context.inputs).toMatchObject({ x2: '6', y2: '0' })
  })

  it('B1B-P23 a camera change re-reads the still cursor', async () => {
    const listeners = new Set()
    const subscribeCamera = vi.fn((listener) => {
      listeners.add(listener)
      listener({ pose: null, viewport: null })
      return () => { listeners.delete(listener) }
    })
    mount(false, null, null, { subscribeCamera })
    await openAndLoad([L('a', [0, 0], [10, 0])])
    onlyModes('endpoint')
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    hover(96, 3)
    expectMarker(10, 0, 'endpoint')
    // One subscription for one viewer, however often the picker renders.
    expect(subscribeCamera).toHaveBeenCalledTimes(1)
    expect(listeners.size).toBe(1)
    holdFrames()
    // Zoomed out under a still cursor (world = client / 20 now): the delivered
    // camera snapshot re-reads it, the endpoint is out of reach and the marker goes...
    viewer.unproject.mockImplementation((cx, cy) => ({ x: cx / 20, y: cy / 20 }))
    act(() => { for (const listener of [...listeners]) listener({ pose: {}, viewport: {} }) })
    expect(lastMarker()).toEqual([null])
    // ...and the click writes the same unsnapped point.
    click(96, 3)
    expect(context.inputs).toMatchObject({ x: '4.8', y: '0.15' })
    // Unmounting releases the subscription.
    cleanup()
    expect(listeners.size).toBe(0)
  })

  it('B1B-P24 a mode switch drops the frame it replaces', async () => {
    mount()
    await openAndLoad([L('a', [0, 0], [10, 0])])
    onlyModes('endpoint')
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    hover(...at(9.6, 0.3))
    const held = holdFrames()
    const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    // A move queues a frame; the mode switch draws now and cancels it.
    hover(...at(9.7, 0.3))
    expect(held.length).toBe(1)
    onlyModes('nearest')
    expect(cancel).toHaveBeenCalledWith(1)
    expectMarker(9.7, 0, 'nearest')
  })

  it('B1B-P14 edge hover and click stay raw', async () => {
    const H = { id: '7', type: 'LINE', layer: 'A', closed: false, editable: true, vertices: [[0, 0, 0], [10, 0, 0]], radius: null, startDeg: null, endDeg: null }
    const V = { id: '9', type: 'LINE', layer: 'B', closed: false, editable: true, vertices: [[5, -5, 0], [5, 5, 0]], radius: null, startDeg: null, endDeg: null }
    mount()
    await openAndLoad([H, V])
    act(() => { context.session.actions.select('7'); context.setOrtho(true) })
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="modify:trim"]'))
    await waitFor(() => expect(screen.getByTestId('cockpit-prompt').getAttribute('data-op')).toBe('trim'))
    // (5.2, 4.8) is 0.28 from V's endpoint (5, 5): an edge step shows no marker.
    hover(52, 48)
    expect(viewer.setSnapMarker).not.toHaveBeenCalled()
    click(52, 48)
    expect(screen.getByLabelText('ribbon edge', { exact: true }).value).toBe('9')
    expect(context.inputs.ex).toBe('5.2')
    expect(context.inputs.ey).toBe('4.8')
    // The point step after it snaps again (and beats ORTHO).
    hover(...at(9.8, 0.3))
    expectMarker(10, 0, 'endpoint')
    click(...at(9.8, 0.3))
    expect(context.inputs).toMatchObject({ x: '10', y: '0' })
  })

  it('B1B-P15 marker identity includes kind and size', async () => {
    mount()
    await openAndLoad([L('a', [0, 0], [5, 0]), { ...C, radius: 3 }])
    // Binary scale, so the aperture is exact: 10 px is 1.25 world units.
    viewer.unproject.mockImplementation((cx, cy) => ({ x: cx / 8, y: cy / 8 }))
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    hover(2, 1)
    expect(lastMarker()).toEqual([{ x: 0, y: 0, kind: 'endpoint' }, 1.25])
    const count = markerCalls().length
    // Same point, kind and size: no call.
    hover(1, 1)
    hover(3, 2)
    expect(markerCalls().length).toBe(count)
    // Same point, another kind: redrawn.
    act(() => { context.setSnapMode('endpoint', false) })
    expect(lastMarker()).toEqual([{ x: 0, y: 0, kind: 'centre' }, 1.25])
    expect(markerCalls().length).toBe(count + 1)
    // Same point and kind, another size (a zoom): redrawn.
    viewer.unproject.mockImplementation((cx, cy) => ({ x: cx / 4, y: cy / 4 }))
    hover(1, 1)
    expect(lastMarker()).toEqual([{ x: 0, y: 0, kind: 'centre' }, 2.5])
    expect(markerCalls().length).toBe(count + 2)
    // A clear resets every cached field: the same marker draws again.
    hover(400, 400)
    expect(lastMarker()).toEqual([null])
    hover(1, 1)
    expect(lastMarker()).toEqual([{ x: 0, y: 0, kind: 'centre' }, 2.5])
    expect(markerCalls().length).toBe(count + 4)
  })

  it('B1B-P16 limitation transitions only', async () => {
    const published = vi.fn()
    window.addEventListener('cockpit:modes', published)
    try {
      mount(false, null, <StatusModesBridge />)
      // 64 parallel lines from y = 0.2 up and one closer at y = 0.125: more
      // than one query examines within the aperture (MAX_LOCAL_PRIMITIVES is 64).
      const lines = [...Array.from({ length: 64 }, (_, i) => L(`h${i}`, [-10, 0.2 + i / 100], [10, 0.2 + i / 100])), L('near', [-10, 0.125], [10, 0.125])]
      await openAndLoad(lines)
      onlyModes('intersection')
      act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
      expect(context.snapLimited).toBe(false)
      // A miss can be limited: parallel lines never intersect.
      published.mockClear()
      hover(0, 0)
      expect(viewer.setSnapMarker).not.toHaveBeenCalled()
      expect(context.snapLimited).toBe(true)
      expect(published).toHaveBeenCalledTimes(1)
      expect(published.mock.calls[0][0].detail.snapLimited).toBe(true)
      // Repeated moves in the same state: no publication, no React update.
      published.mockClear()
      const renders = probeRenders
      hover(1, 1)
      hover(2, 1)
      hover(1, 2)
      expect(published).not.toHaveBeenCalled()
      expect(probeRenders).toBe(renders)
      // Far from everything: unlimited, once.
      hover(500, 500)
      expect(context.snapLimited).toBe(false)
      expect(published).toHaveBeenCalledTimes(1)
      hover(510, 500)
      expect(published).toHaveBeenCalledTimes(1)
      // A hit can be limited too, and the bounded query still keeps the closest.
      onlyModes('nearest')
      hover(0, 0)
      expectMarker(0, 0.125, 'nearest')
      expect(context.snapLimited).toBe(true)
      // Clear paths: the master off, then on again at the same cursor.
      pressF3()
      expect(context.snapLimited).toBe(false)
      expect(lastMarker()).toEqual([null])
      pressF3()
      expect(context.snapLimited).toBe(true)
      // Leaving the ground.
      act(() => { ground.dispatchEvent(new MouseEvent('pointerleave')) })
      expect(context.snapLimited).toBe(false)
      hover(0, 0)
      expect(context.snapLimited).toBe(true)
      // Cancelling the pointer.
      act(() => { ground.dispatchEvent(new MouseEvent('pointercancel')) })
      expect(context.snapLimited).toBe(false)
      hover(0, 0)
      expect(context.snapLimited).toBe(true)
      // Losing the pick sequence.
      act(() => { context.setArmed(null) })
      expect(context.snapLimited).toBe(false)
      act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
      hover(0, 0)
      expect(context.snapLimited).toBe(true)
      // An edge step bypasses the query.
      act(() => { context.setArmed({ group: 'groups', op: 'group' }) })
      expect(context.snapLimited).toBe(false)
      hover(1, 1)
      expect(context.snapLimited).toBe(false)
    } finally {
      window.removeEventListener('cockpit:modes', published)
    }
  })

  it('B1B-P17 insertion index with no fixed points', async () => {
    mount()
    await openAndLoad([
      { id: 't1', type: 'TEXT', layer: '0', vertices: [[3, 4, 0]] },
      { id: 'i1', type: 'INSERT', layer: '0', ip: [8, 1, 0], vertices: [[8, 1, 0]] },
    ])
    onlyModes('insertion')
    act(() => { context.setArmed({ group: 'draw', op: 'createLine' }) })
    hover(...at(3.1, 4))
    expectMarker(3, 4, 'insertion')
    click(...at(3.1, 4))
    hover(...at(7.9, 1.2))
    expectMarker(8, 1, 'insertion')
    click(...at(7.9, 1.2))
    expect(context.inputs).toMatchObject({ x: '3', y: '4', x2: '8', y2: '1' })
  })

  it('B1B-P18 snap beats ortho and cleanup clears feedback', async () => {
    mount()
    await openAndLoad([L('a', [0, 0], [10, 3])])
    act(() => { context.setOrtho(true); context.setArmed({ group: 'draw', op: 'createLine', from: [0, 0] }) })
    // ORTHO alone would hold (10.2, 0); the endpoint (10, 3) wins.
    hover(...at(10.2, 3.1))
    expectMarker(10, 3, 'endpoint')
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[0, 0], [10, 3]], false)
    click(...at(10.2, 3.1))
    expect(context.inputs).toMatchObject({ x2: '10', y2: '3' })
    // With nothing in reach ORTHO holds again.
    act(() => { context.setArmed({ group: 'draw', op: 'createLine', from: [0, 0] }, { rearm: true }) })
    hover(...at(6, 1))
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[0, 0], [6, 0]], false)
    hover(...at(10.2, 3.1))
    expectMarker(10, 3, 'endpoint')
    cleanup()
    expect(lastMarker()).toEqual([null])
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith(null)
  })
})
