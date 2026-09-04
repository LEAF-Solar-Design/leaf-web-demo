// W4g-4: the reference's Modify verbs the crate carries (COPY, MIRROR,
// ROTATE, SCALE, EXPLODE) and RECTANG, end to end on the client: the
// store's operand reading, the command words, the pick sequences and ghosts,
// and the ribbon prompts posting the exact edit the worker expects.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CadEditSurface from './CadEditSurface.jsx'
import EngineRibbonClusters, { PROMPTS } from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { CREATE_OPS, CREATING_EDITS, buildCreatePayload, buildEditPayload } from './engineSession.js'
import { PICK_SEQUENCES, applyPick, ghostFor, startPicking } from './pointPicking.js'
import { parseDrawingCommand } from '../lib/commandWords.js'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

describe('W4g-4 store: operand reading for the new verbs', () => {
  it('copy reads dx/dy like move; mirror wants two distinct points and a keep flag; rotate an angle; scale a positive factor', () => {
    expect(buildEditPayload('copy', 'e1', { dx: '5', dy: '-2' })).toEqual({ payload: { entityId: 'e1', dx: 5, dy: -2 } })
    expect(buildEditPayload('copy', 'e1', { dx: 'x', dy: '0' }).refusal).toBe('Copy refused: dx and dy must both be numbers.')
    expect(buildEditPayload('mirror', 'e1', { x1: '0', y1: '0', x2: '0', y2: '10', keep: 'true' }))
      .toEqual({ payload: { entityId: 'e1', x1: 0, y1: 0, x2: 0, y2: 10, keep: true } })
    expect(buildEditPayload('mirror', 'e1', { x1: '0', y1: '0', x2: '0', y2: '10', keep: 'false' }).payload.keep).toBe(false)
    expect(buildEditPayload('mirror', 'e1', { x1: '1', y1: '1', x2: '1', y2: '1' }).refusal).toBe('Mirror refused: the two points of the mirror line must differ.')
    expect(buildEditPayload('mirror', 'e1', { x1: '1', y1: 'a', x2: '2', y2: '2' }).refusal).toBe('Mirror refused: x1, y1, x2 and y2 must all be numbers.')
    expect(buildEditPayload('rotate', 'e1', { cx: '5', cy: '5', deg: '90' })).toEqual({ payload: { entityId: 'e1', cx: 5, cy: 5, deg: 90 } })
    expect(buildEditPayload('rotate', 'e1', { cx: '5', cy: '5', deg: 'ninety' }).refusal).toBe('Rotate refused: the angle must be a number (degrees).')
    expect(buildEditPayload('rotate', 'e1', { cx: '', cy: '5', deg: '1' }).refusal).toBe('Rotate refused: the base point x and y must both be numbers.')
    expect(buildEditPayload('scale', 'e1', { cx: '0', cy: '0', factor: '2.5' })).toEqual({ payload: { entityId: 'e1', cx: 0, cy: 0, factor: 2.5 } })
    expect(buildEditPayload('scale', 'e1', { cx: '0', cy: '0', factor: '0' }).refusal).toBe('Scale refused: the factor must be greater than 0.')
    expect(buildEditPayload('scale', 'e1', { cx: '0', cy: '0', factor: '-1' }).refusal).toBe('Scale refused: the factor must be greater than 0.')
    expect(buildEditPayload('explode', 'e1', {})).toEqual({ payload: { entityId: 'e1' } })
  })

  it('RECTANG lowers two corners to the closed four-point polyline the engine draws, and refuses a degenerate one', () => {
    expect(CREATE_OPS).toContain('createRectangle')
    expect(buildCreatePayload('createRectangle', { x: '0', y: '0', x2: '4', y2: '3', layer: 'P' }))
      .toEqual({ payload: { points: [0, 0, 4, 0, 4, 3, 0, 3], closed: true, layer: 'P' } })
    expect(buildCreatePayload('createRectangle', { x: '0', y: '0', x2: '0', y2: '3' }).refusal).toBe('Rectangle refused: the corners must differ in both x and y.')
    expect(buildCreatePayload('createRectangle', { x: '0', y: 'q', x2: '1', y2: '3' }).refusal).toBe('Rectangle refused: x, y, x2 and y2 must all be numbers.')
    expect(CREATING_EDITS).toEqual(['copy', 'mirror', 'explode', 'arrayRect', 'arrayPolar'])
  })
})

describe('W4g-4 words, picks and ghosts', () => {
  it('the reference words arm the verbs', () => {
    expect(parseDrawingCommand('co')).toMatchObject({ group: 'modify', op: 'copy', verb: 'COPY' })
    expect(parseDrawingCommand('COPY')).toMatchObject({ op: 'copy' })
    expect(parseDrawingCommand('mi')).toMatchObject({ op: 'mirror', verb: 'MIRROR' })
    expect(parseDrawingCommand('ro')).toMatchObject({ op: 'rotate', verb: 'ROTATE' })
    expect(parseDrawingCommand('sc')).toMatchObject({ op: 'scale', verb: 'SCALE' })
    expect(parseDrawingCommand('x')).toMatchObject({ op: 'explode', verb: 'EXPLODE' })
    expect(parseDrawingCommand('rec')).toMatchObject({ group: 'draw', op: 'createRectangle', verb: 'RECTANG' })
    expect(parseDrawingCommand('e')).toMatchObject({ op: 'delete', verb: 'ERASE' })
  })

  it('copy picks base then displacement; mirror two points; rotate and scale the base; rectangle two corners with a rectangle ghost', () => {
    expect(PICK_SEQUENCES.copy.map((s) => s.kind)).toEqual(['base', 'delta'])
    expect(PICK_SEQUENCES.mirror.map((s) => s.keys)).toEqual([['x1', 'y1'], ['x2', 'y2']])
    expect(PICK_SEQUENCES.rotate[0].keys).toEqual(['cx', 'cy'])
    expect(PICK_SEQUENCES.scale[0].keys).toEqual(['cx', 'cy'])
    let state = startPicking('createRectangle')
    const first = applyPick(state, 2, 3, {})
    state = first.state
    expect(first.writes).toEqual([['x', '2'], ['y', '3']])
    expect(ghostFor(state, 6, 8)).toEqual({ pts: [[2, 3], [6, 3], [6, 8], [2, 8]], closed: true })
    const second = applyPick(state, 6, 8, {})
    expect(second.writes).toEqual([['x2', '6'], ['y2', '8']])
    expect(ghostFor(second.state, 9, 9)).toBeNull()
    let m = startPicking('mirror')
    m = applyPick(m, 0, 0, {}).state
    expect(ghostFor(m, 0, 10)).toEqual({ pts: [[0, 0], [0, 10]], closed: false })
    expect(PROMPTS.mirror.steps.map((s) => s.ask)).toEqual([
      'Specify first point of mirror line:', 'Specify second point of mirror line:', 'Keep source:',
    ])
    expect(PROMPTS.explode).toBeUndefined()
  })
})

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const POLY = { id: '9', type: 'LWPOLYLINE', layer: 'P', closed: true, vertices: [[0, 0, 0], [4, 0, 0], [4, 3, 0]], radius: null, startDeg: null, endDeg: null }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
let handle
function mount() {
  workers = []
  handle = {}
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  function Probe() { handle.context = useEngineSessionContext(); return null }
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
      </DraftingRibbon>
      <CadEditSurface enabled />
    </EngineSessionProvider>,
  )
  return handle
}

async function openAndLoad(entities) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf()] } })
    await Promise.resolve(); await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBe(1))
  workers[0].emit({ type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [] })
}

const tool = (label) => screen.getByRole('button', { name: label })
const field = (label) => screen.getByLabelText(`ribbon ${label}`, { exact: true })
const lastPost = () => workers[0].posted.filter((m) => m.type === 'applyEdit').pop()
const runPrompt = () => { fireEvent.click(screen.getByRole('button', { name: 'Run' })) }

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:verbs')
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup() })

describe('W4g-4 ribbon: each verb posts the exact edit, and a creating verb selects what it made', () => {
  it('copy, mirror, rotate and scale arm a prompt and post their payloads; explode runs on click', async () => {
    const studio = mount()
    await openAndLoad([POLY])
    act(() => { studio.context.session.actions.select('9') })
    fireEvent.click(tool('copy'))
    fireEvent.change(field('dx'), { target: { value: '3' } })
    fireEvent.change(field('dy'), { target: { value: '4' } })
    runPrompt()
    expect(lastPost()).toEqual({ type: 'applyEdit', op: 'copy', payload: { entityId: '9', dx: 3, dy: 4 } })
    // The copy lands as a new entity and the selection moves to it.
    const MADE = { ...POLY, id: '10', vertices: [[3, 4, 0], [7, 4, 0], [7, 7, 0]] }
    workers[0].emit({ type: 'editApplied', op: 'copy', ok: true, entities: [POLY, MADE], entityCount: 2, createdId: '10', bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(studio.context.session.selectedId).toBe('10')
    expect(studio.context.session.status).toContain('copy applied: entity 10 drawn')

    fireEvent.click(tool('mirror'))
    fireEvent.change(field('x1'), { target: { value: '0' } })
    fireEvent.change(field('y1'), { target: { value: '0' } })
    fireEvent.change(field('x2'), { target: { value: '0' } })
    fireEvent.change(field('y2'), { target: { value: '10' } })
    runPrompt()
    expect(lastPost()).toEqual({ type: 'applyEdit', op: 'mirror', payload: { entityId: '10', x1: 0, y1: 0, x2: 0, y2: 10, keep: true } })
    workers[0].emit({ type: 'editApplied', op: 'mirror', ok: true, entities: [POLY, MADE], entityCount: 2, bytes: new Uint8Array([48, 11]), byteLength: 2 })

    fireEvent.click(tool('rotate'))
    fireEvent.change(field('cx'), { target: { value: '1' } })
    fireEvent.change(field('cy'), { target: { value: '2' } })
    fireEvent.change(field('angle'), { target: { value: '45' } })
    runPrompt()
    expect(lastPost()).toEqual({ type: 'applyEdit', op: 'rotate', payload: { entityId: '10', cx: 1, cy: 2, deg: 45 } })
    workers[0].emit({ type: 'editApplied', op: 'rotate', ok: true, entities: [POLY, MADE], entityCount: 2, bytes: new Uint8Array([48, 12]), byteLength: 2 })

    fireEvent.click(tool('scale'))
    fireEvent.change(field('factor'), { target: { value: '0.5' } })
    runPrompt()
    expect(lastPost()).toEqual({ type: 'applyEdit', op: 'scale', payload: { entityId: '10', cx: 1, cy: 2, factor: 0.5 } })
    workers[0].emit({ type: 'editApplied', op: 'scale', ok: true, entities: [POLY, MADE], entityCount: 2, bytes: new Uint8Array([48, 13]), byteLength: 2 })

    // EXPLODE has no operands: the click is the run.
    fireEvent.click(tool('explode'))
    expect(lastPost()).toEqual({ type: 'applyEdit', op: 'explode', payload: { entityId: '10' } })
    const parts = [{ ...POLY, id: '11', type: 'LINE', vertices: [[3, 4, 0], [7, 4, 0]] }, { ...POLY, id: '12', type: 'LINE', vertices: [[7, 4, 0], [7, 7, 0]] }]
    workers[0].emit({ type: 'editApplied', op: 'explode', ok: true, entities: [POLY, ...parts], entityCount: 3, createdId: '11', createdIds: ['11', '12'], bytes: new Uint8Array([48, 14]), byteLength: 2 })
    expect(studio.context.session.selectedId).toBe('11')
    expect(studio.context.session.entityCount).toBe(3)
  })

  it('RECTANG arms a two-corner prompt and posts the lowered polyline as createPolyline', async () => {
    mount()
    await openAndLoad([POLY])
    fireEvent.click(tool('rectangle'))
    fireEvent.change(field('x'), { target: { value: '1' } })
    fireEvent.change(field('y'), { target: { value: '1' } })
    fireEvent.change(field('x2'), { target: { value: '5' } })
    fireEvent.change(field('y2'), { target: { value: '4' } })
    runPrompt()
    expect(lastPost()).toEqual({ type: 'applyEdit', op: 'createPolyline', payload: { points: [1, 1, 5, 1, 5, 4, 1, 4], closed: true, layer: '' } })
  })
})
