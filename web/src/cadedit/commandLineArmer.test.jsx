// W4f slice B: a typed command word, delivered as the ONE cockpit:command
// window event, arms the same prompt a ribbon click arms; ERASE runs on a live
// selection and does nothing without one; anything malformed is ignored.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { byId } from '../lib/actionRegistry.js'
import { COCKPIT_COMMAND_EVENT, parseDrawingCommand } from '../lib/commandWords.js'

import CadEditSurface from './CadEditSurface.jsx'
import CommandLineArmer, { acceptsCommand } from './CommandLineArmer.jsx'
import CanvasPointPicker from './CanvasPointPicker.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider from './EngineSessionProvider.jsx'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
function mount(picker = null) {
  workers = []
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
        <CommandLineArmer />
      </DraftingRibbon>
      {picker && <CanvasPointPicker {...picker} />}
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

const command = (detail) => act(() => { window.dispatchEvent(new CustomEvent(COCKPIT_COMMAND_EVENT, { detail })) })
const promptEl = () => screen.queryByTestId('cockpit-prompt')
const point = (text) => {
  const detail = { text, handled: false }
  act(() => window.dispatchEvent(new CustomEvent('cockpit:point', { detail })))
  return detail.handled
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('CommandLineArmer (W4f slice B)', () => {
  it.each([false, true])('keeps a refused LINE endpoint correctable with picker=%s', async (withPicker) => {
    const ground = document.createElement('div')
    mount(withPicker ? { ground, viewerRef: { current: {} } } : null)
    await openAndLoad()
    command(parseDrawingCommand('LINE'))
    const layer = screen.getByLabelText('ribbon layer').value
    point('0,0')
    point('0,0')
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(0)
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify next point:')
    expect(screen.getByTestId('cockpit-prompt-note').textContent).toContain('refused')
    point('10,0')
    expect(screen.getByLabelText('ribbon layer').value).toBe(layer)
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', op: 'createLine', payload: { x1: 0, y1: 0, x2: 10, y2: 0 } })
  })

  it.each(['bar/click/bar', 'click/bar/bar'])('chains two segments through one point step: %s', async (order) => {
    const ground = document.createElement('div')
    const viewer = { unproject: (x, y) => ({ x, y }), setRubberBand: vi.fn() }
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(); return 0 })
    mount({ ground, viewerRef: { current: viewer } })
    await openAndLoad()
    act(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F3' })))
    const click = (x, y) => act(() => {
      ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: x, clientY: y, button: 0 }))
      ground.dispatchEvent(new MouseEvent('pointerup', { clientX: x, clientY: y, button: 0 }))
    })
    command(parseDrawingCommand('LINE'))
    if (order === 'bar/click/bar') { point('5,5'); click(10, 0) }
    else { click(5, 5); point('10,0') }
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(1)
    expect(workers[0].posted.at(-1)).toMatchObject({ payload: { x1: 5, y1: 5, x2: 10, y2: 0 } })
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, createdId: 'e2', entities: [LINE, { ...LINE, id: 'e2', vertices: [[5, 5], [10, 0]] }], entityCount: 2 })
    point('20,0')
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(2)
    expect(workers[0].posted.at(-1)).toMatchObject({ payload: { x1: 10, y1: 0, x2: 20, y2: 0 } })
    expect(screen.getByLabelText('ribbon layer').value).toBe('')
  })

  it('takes absolute, relative and polar points through the armed operand and publishes its live ask', async () => {
    mount()
    await openAndLoad()
    expect(point('0,0')).toBe(false)
    command(parseDrawingCommand('LINE'))
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify first point:')
    expect(point('@10,0')).toBe(true)
    expect(screen.getByRole('status').textContent).toContain('needs a previous point')
    expect(screen.getByLabelText('ribbon x').value).toBe('@10,0')
    expect(point('0,0')).toBe(true)
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify next point:')
    expect(point('@10,0')).toBe(true)
    expect(screen.getByLabelText('ribbon x2').value).toBe('10')
    expect(screen.getByLabelText('ribbon y2').value).toBe('0')
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', op: 'createLine', payload: { x1: 0, y1: 0, x2: 10, y2: 0 } })
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, createdId: 'e2', entities: [LINE, { ...LINE, id: 'e2', vertices: [[0, 0], [10, 0]] }], entityCount: 2 })
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify next point:')
    expect(point('10<90')).toBe(true)
    expect(screen.getByLabelText('ribbon x2').value).toBe('10')
    expect(screen.getByLabelText('ribbon y2').value).toBe('10')
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', op: 'createLine', payload: { x1: 10, y1: 0, x2: 10, y2: 10 } })
  })

  it('keeps a point typed for a scalar and names the field without advancing', async () => {
    mount()
    await openAndLoad()
    command(parseDrawingCommand('CIRCLE'))
    point('0,0')
    expect(point('10,10')).toBe(true)
    expect(screen.getByLabelText('ribbon r').value).toBe('10,10')
    expect(screen.getByRole('status').textContent).toContain('r needs a scalar')
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('CIRCLE  Specify radius:')
    expect(screen.getByTestId('cockpit-prompt-run')).toBeDisabled()
  })

  it('a draw word arms its prompt like the ribbon click; a second word re-arms; malformed details are ignored', async () => {
    mount()
    await openAndLoad()
    expect(promptEl()).toBeNull()
    command({ group: 'draw', op: 'createLine' })
    expect(promptEl().getAttribute('data-op')).toBe('createLine')
    expect(promptEl().textContent).toContain('LINE')
    command({ group: 'draw', op: 'createCircle' })
    expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    for (const bad of [null, 'createLine', { group: 'draw' }, { group: 'nope', op: 'createLine' }, { group: 'draw', op: 'format' }, { group: 'draw', op: 'constructor' }]) {
      command(bad)
      expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    }
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(0)
  })

  it('ERASE runs at once on a live selection and does nothing without one', async () => {
    mount()
    await openAndLoad()
    const before = workers[0].posted.length
    command({ group: 'modify', op: 'delete' })
    expect(workers[0].posted.length).toBe(before)
    expect(promptEl()).toBeNull()
    fireEvent.click(screen.getByRole('radio'))
    command({ group: 'modify', op: 'delete' })
    const posted = workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } })
  })

  it('C2: typed x runs EXPLODE at once on a selected LWPOLYLINE', async () => {
    mount()
    await openAndLoad([{ ...LINE, type: 'LWPOLYLINE', vertices: [[0, 0], [1, 0], [1, 1]] }])
    fireEvent.click(screen.getByRole('radio'))
    const before = workers[0].posted.length
    command(parseDrawingCommand('x'))
    expect(workers[0].posted.slice(before)).toEqual([{ type: 'applyEdit', op: 'explode', payload: { entityId: 'e1' } }])
    expect(promptEl()).toBeNull()
  })

  it.each([
    ['INSERT', 'an INSERT is placed, not edited, in this round'],
    ['DIMENSION', 'a dimension is placed, not edited, in this round'],
  ])('C2: typed explode on %s surfaces the placed-kind refusal without posting', async (type, sentence) => {
    mount()
    await openAndLoad([{ ...LINE, type, editable: false }])
    fireEvent.click(screen.getByRole('radio'))
    const before = workers[0].posted.length
    command(parseDrawingCommand('explode'))
    expect(screen.getByRole('status').textContent).toBe(sentence)
    expect(workers[0].posted).toHaveLength(before)
    expect(promptEl()).toBeNull()
  })

  it('C2: typed x without a selection surfaces the ladder sentence and arms nothing', async () => {
    mount()
    await openAndLoad()
    const before = workers[0].posted.length
    command(parseDrawingCommand('x'))
    expect(screen.getByRole('status').textContent).toBe('select an entity in the drawing')
    expect(workers[0].posted).toHaveLength(before)
    expect(promptEl()).toBeNull()
  })

  it('F4: typed m arms MOVE on INSERT and typed erase posts delete', async () => {
    mount()
    await openAndLoad([{ id: '11', type: 'INSERT', name: 'Fixture', ip: [0, 0, 0], rotationDeg: 0, scale: [1, 1, 1], layer: '0', editable: false }])
    fireEvent.click(screen.getByRole('radio'))
    const before = workers[0].posted.length
    command(parseDrawingCommand('m'))
    expect(promptEl().getAttribute('data-op')).toBe('move')
    expect(screen.getByTestId('cockpit-prompt-note').textContent).toBe('an INSERT is placed, not edited, in this round')
    expect(workers[0].posted).toHaveLength(before)
    command(parseDrawingCommand('erase'))
    expect(workers[0].posted.slice(before)).toEqual([{ type: 'applyEdit', op: 'delete', payload: { entityId: '11' } }])
  })

  // W4g-5c: the clipboard words. kimi on #1025 found them registered as words
  // and dropped here, because this gate knew two groups; the ribbon arm had
  // the same defect one layer down. Both layers are pinned now.
  it('PASTECLIP arms the PASTE prompt; COPYCLIP copies without touching the engine; CUTCLIP posts the delete', async () => {
    mount()
    await openAndLoad()
    command({ group: 'clipboard', op: 'pasteClip' })
    expect(promptEl()).not.toBeNull()
    expect(promptEl().getAttribute('data-op')).toBe('pasteClip')
    expect(promptEl().textContent).toContain('PASTE')
    // Nothing copied yet: the prompt reads the clipboard ladder and holds Run.
    expect(promptEl().textContent).toContain('nothing on the clipboard yet')
    fireEvent.click(screen.getByRole('radio'))
    const before = workers[0].posted.length
    command({ group: 'clipboard', op: 'copyClip' })
    expect(workers[0].posted.length).toBe(before)
    expect(screen.getByRole('status').textContent).toContain('is on the clipboard')
    command({ group: 'clipboard', op: 'cutClip' })
    const posted = workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } })
  })

  it.each(['g', 'GROUP', 'UNGROUP'])('%s arms the real Groups prompt without posting an edit', async (word) => {
    mount()
    await openAndLoad()
    const detail = parseDrawingCommand(word)
    const op = word === 'UNGROUP' ? 'ungroup' : 'group'
    expect(detail).toMatchObject({ group: 'groups', op })
    expect(byId(`groups:${op}`)).toMatchObject({ surface: 'engine', group: 'groups', panel: 'groups', op })
    const before = workers[0].posted.length
    command(detail)
    expect(promptEl().getAttribute('data-op')).toBe(op)
    expect(promptEl().textContent).toContain(op === 'group' ? 'Select objects to add:' : 'Enter group name:')
    expect(workers[0].posted).toHaveLength(before)
  })

  it('LEADER arms the live createMleader prompt', async () => {
    mount()
    await openAndLoad()
    expect(promptEl()).toBeNull()
    command(parseDrawingCommand('LEADER'))
    expect(promptEl().getAttribute('data-op')).toBe('createMleader')
    // A mismatched reason (never emitted by the real parser, but the gate
    // must fail closed against it anyway) is dropped, same as any malformed detail.
    command({ group: 'deferred', op: 'leader', reason: 'a made-up sentence' })
    expect(promptEl().getAttribute('data-op')).toBe('createMleader')
  })

  it('acceptsCommand is the fail-closed gate', () => {
    expect(acceptsCommand({ group: 'groups', op: 'group' })).toBe(true)
    expect(acceptsCommand({ group: 'groups', op: 'ungroup' })).toBe(true)
    expect(acceptsCommand({ group: 'deferred', op: 'group' })).toBe(false)
    expect(acceptsCommand({ group: 'clipboard', op: 'pasteClip' })).toBe(true)
    expect(acceptsCommand({ group: 'clipboard', op: 'copyClip' })).toBe(true)
    expect(acceptsCommand({ group: 'clipboard', op: 'cutClip' })).toBe(true)
    expect(acceptsCommand({ group: 'annotation', op: 'text' })).toBe(false)
    expect(acceptsCommand({ group: 'draw', op: 'createArc' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'delete' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'hasOwnProperty' })).toBe(false)
    expect(acceptsCommand({ group: 'draw', op: 'delete' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'undo' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'redo' })).toBe(true)
    expect(acceptsCommand({ group: 'view', op: 'fit' })).toBe(false)
    expect(acceptsCommand(undefined)).toBe(false)
  })
})
