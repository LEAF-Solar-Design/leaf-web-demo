// W4g-6: TRIM / EXTEND / FILLET / CHAMFER end to end on the client: the
// store's operand reading (live, before any geometry), the planner over the
// session's entities, the lowering to the worker's payloads, the words, the
// pick sequences with the EDGE kind, the ribbon prompts posting ONE batch,
// and the batch reply selecting what it made under the verb's own name.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CadEditSurface from './CadEditSurface.jsx'
import EngineRibbonClusters, { PROMPTS } from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { CREATING_EDITS, INTERSECT_VERBS, buildEditPayload, lowerSteps, planIntersectVerb } from './engineSession.js'
import { MAX_BATCH_STEPS } from './intersect.js'
import { PICK_SEQUENCES, applyPick, ghostFor, startPicking } from './pointPicking.js'
import { parseDrawingCommand } from '../lib/commandWords.js'
import { forGroup } from '../lib/actionRegistry.js'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

const H = { id: '7', handle: '7', index: 0, type: 'LINE', layer: 'A', closed: false, editable: true, vertices: [[0, 0, 0], [10, 0, 0]], radius: null, startDeg: null, endDeg: null }
const V = { id: '9', handle: '9', index: 1, type: 'LINE', layer: 'A', closed: false, editable: true, vertices: [[5, -5, 0], [5, 5, 0]], radius: null, startDeg: null, endDeg: null }
const ARC = { id: '11', handle: '11', index: 2, type: 'ARC', layer: 'A', closed: false, editable: true, vertices: [[8, 2, 0]], radius: 2, startDeg: 270, endDeg: 0 }
const session = (entities, selectedId) => ({ entities, selectedId })

describe('W4g-6 store: the operands are read live, before any geometry', () => {
  it('every verb needs a second entity other than the selection and a numeric point', () => {
    expect(buildEditPayload('trim', '7', { edge: '', x: '1', y: '1' }).refusal).toBe('Trim refused: select the cutting edge by clicking it on the drawing.')
    expect(buildEditPayload('trim', '7', { edge: '7', x: '1', y: '1' }).refusal).toBe('Trim refused: the cutting edge must be a different entity from the selection.')
    expect(buildEditPayload('trim', '7', { edge: '9', x: 'a', y: '1' }).refusal).toBe('Trim refused: the point on the part to remove: x and y must both be numbers.')
    expect(buildEditPayload('trim', '7', { edge: ' 9 ', x: '8', y: '0' })).toEqual({ payload: { entityId: '7', edge: '9', x: 8, y: 0 } })
    expect(buildEditPayload('extend', '7', { edge: '', x: '1', y: '1' }).refusal).toBe('Extend refused: select the boundary edge by clicking it on the drawing.')
    expect(buildEditPayload('extend', '7', { edge: '9', x: '1', y: '' }).refusal).toBe('Extend refused: the point near the end to extend: x and y must both be numbers.')
  })

  it('fillet reads a radius of 0 or more and chamfer two distances, plus the point on the second entity', () => {
    expect(buildEditPayload('fillet', '7', { edge: '9', x: '1', y: '1', r: 'r', ex: '5', ey: '3' }).refusal).toBe('Fillet refused: the radius must be a number.')
    expect(buildEditPayload('fillet', '7', { edge: '9', x: '1', y: '1', r: '-1', ex: '5', ey: '3' }).refusal).toBe('Fillet refused: the radius must be 0 or more.')
    expect(buildEditPayload('fillet', '7', { edge: '9', x: '1', y: '1', r: '2', ex: '', ey: '3' }).refusal).toBe('Fillet refused: the point on the second object (edge x, edge y) must both be numbers.')
    expect(buildEditPayload('fillet', '7', { edge: '9', x: '1', y: '1', r: '0', ex: '5', ey: '3' })).toEqual({ payload: { entityId: '7', edge: '9', x: 1, y: 1 } })
    expect(buildEditPayload('chamfer', '7', { edge: '9', x: '1', y: '1', d1: '1', d2: 'two', ex: '5', ey: '3' }).refusal).toBe('Chamfer refused: both distances must be numbers.')
    expect(buildEditPayload('chamfer', '7', { edge: '9', x: '1', y: '1', d1: '1', d2: '-2', ex: '5', ey: '3' }).refusal).toBe('Chamfer refused: both distances must be 0 or more.')
    expect(buildEditPayload('chamfer', '7', { edge: '9', x: '1', y: '1', d1: '1', d2: '2', ex: '5', ey: 'q' }).refusal).toBe('Chamfer refused: the point on the second line (edge x, edge y) must both be numbers.')
    expect(Object.keys(INTERSECT_VERBS)).toEqual(['trim', 'extend', 'fillet', 'chamfer'])
    // A batch's creates select what they made, like the other creating edits.
    expect(CREATING_EDITS).toContain('batch')
  })
})

describe('W4g-6 store: the planner over the session and the lowering to the worker', () => {
  it('plans a trim from the entities by id and refuses an entity that is gone', () => {
    expect(planIntersectVerb('trim', session([H, V], '7'), { edge: '9', x: '8', y: '0' })).toEqual({
      steps: [{ op: 'setVertices', entityId: '7', points: [[0, 0], [5, 0]], closed: false }],
    })
    expect(planIntersectVerb('trim', session([V], '7'), { edge: '9', x: '8', y: '0' }).refusal).toBe('Trim refused: the selected entity is no longer in the document.')
    expect(planIntersectVerb('trim', session([H], '7'), { edge: '9', x: '8', y: '0' }).refusal).toBe('Trim refused: the cutting edge is no longer in the document.')
    expect(planIntersectVerb('trim', session([H, V], '7'), { edge: '', x: '8', y: '0' }).refusal).toMatch(/select the cutting edge/)
    // The geometry's own refusal comes through the same door.
    expect(planIntersectVerb('trim', session([H, V], '7'), { edge: '9', x: '5', y: '0' }).refusal).toBe('Trim refused: click on the part to remove, away from the crossing.')
    expect(planIntersectVerb('nope', session([H, V], '7'), {}).refusal).toBe('Edit refused: unknown operation nope.')
  })

  it('plans a fillet and a chamfer between the selection and the second line', () => {
    const Y = { ...V, id: '9', vertices: [[10, 0, 0], [10, 10, 0]] }
    expect(planIntersectVerb('fillet', session([H, Y], '7'), { edge: '9', r: '2', x: '2', y: '0', ex: '10', ey: '8' })).toEqual({
      steps: [
        { op: 'setVertices', entityId: '7', points: [[0, 0], [8, 0]], closed: false },
        { op: 'setVertices', entityId: '9', points: [[10, 2], [10, 10]], closed: false },
        { op: 'createArc', inputs: { x: 8, y: 2, r: 2, a0: 270, a1: 0, layer: 'A' } },
      ],
    })
    expect(planIntersectVerb('chamfer', session([H, Y], '7'), { edge: '9', d1: '2', d2: '3', x: '2', y: '0', ex: '10', ey: '8' }).steps[2])
      .toEqual({ op: 'createLine', inputs: { x: 8, y: 0, x2: 10, y2: 3, layer: 'A' } })
    expect(planIntersectVerb('extend', session([{ ...H, vertices: [[0, 0, 0], [4, 0, 0]] }, Y], '7'), { edge: '9', x: '4', y: '0' })).toEqual({
      steps: [{ op: 'setVertices', entityId: '7', points: [[0, 0], [10, 0]], closed: false }],
    })
  })

  it('lowers every step through the same builders a single op uses, bounded and refusing the first bad one', () => {
    expect(lowerSteps([
      { op: 'setVertices', entityId: '7', points: [[0, 0], [8, 0]], closed: false },
      { op: 'setArc', entityId: '11', x: 8, y: 2, r: 2, a0: 270, a1: 0 },
      { op: 'delete', entityId: '9' },
      { op: 'createArc', inputs: { x: 8, y: 2, r: 2, a0: 270, a1: 0, layer: 'A' } },
    ])).toEqual({
      steps: [
        { op: 'setVertices', payload: { entityId: '7', points: [0, 0, 8, 0], closed: false } },
        { op: 'setArc', payload: { entityId: '11', cx: 8, cy: 2, radius: 2, startDeg: 270, endDeg: 0 } },
        { op: 'delete', payload: { entityId: '9' } },
        { op: 'createArc', payload: { cx: 8, cy: 2, radius: 2, startDeg: 270, endDeg: 0, layer: 'A' } },
      ],
    })
    expect(lowerSteps([]).refusal).toBe('Edit refused: the plan has no steps.')
    expect(lowerSteps(Array.from({ length: MAX_BATCH_STEPS + 1 }, () => ({ op: 'delete', entityId: '9' }))).refusal).toBe(`Edit refused: the plan has more than ${MAX_BATCH_STEPS} steps.`)
    expect(lowerSteps([{ op: 'setVertices', entityId: '7', points: [[0, 0]], closed: false }]).refusal).toMatch(/needs 2 to 1000 points/)
    expect(lowerSteps([{ op: 'setVertices', entityId: '7', points: [[0, 0], [Number.NaN, 1]], closed: false }]).refusal).toBe('Edit refused: a geometry step has a point that is not a number.')
    expect(lowerSteps([{ op: 'setArc', entityId: '11', x: 0, y: 0, r: 0, a0: 0, a1: 90 }]).refusal).toBe('Edit refused: an arc step needs a radius greater than 0.')
    expect(lowerSteps([{ op: 'setArc', entityId: '11', x: 0, y: 0, r: 1, a0: 0, a1: 360 }]).refusal).toBe('Edit refused: an arc step needs a start and end that differ.')
    expect(lowerSteps([{ op: 'delete', entityId: '' }]).refusal).toBe('Edit refused: step delete names no entity.')
    expect(lowerSteps([{ op: 'createLine', inputs: { x: '0', y: '0', x2: '0', y2: '0' } }]).refusal).toBe('Line refused: the two points must differ.')
    expect(lowerSteps([{ op: 'explodeAll', entityId: '7' }]).refusal).toBe('Edit refused: unknown step explodeAll.')
  })
})

describe('W4g-6 words, records, picks and prompts', () => {
  it('the reference aliases arm the verbs and the four records sit in Modify', () => {
    expect(parseDrawingCommand('tr')).toMatchObject({ group: 'modify', op: 'trim', verb: 'TRIM' })
    expect(parseDrawingCommand('TRIM')).toMatchObject({ op: 'trim' })
    expect(parseDrawingCommand('ex')).toMatchObject({ op: 'extend', verb: 'EXTEND' })
    expect(parseDrawingCommand('f')).toMatchObject({ op: 'fillet', verb: 'FILLET' })
    expect(parseDrawingCommand('cha')).toMatchObject({ op: 'chamfer', verb: 'CHAMFER' })
    expect(parseDrawingCommand('chamfer')).toMatchObject({ op: 'chamfer' })
    const ids = forGroup('modify').map((a) => a.id)
    for (const id of ['modify:trim', 'modify:extend', 'modify:fillet', 'modify:chamfer']) expect(ids).toContain(id)
    expect(forGroup('modify').find((a) => a.op === 'fillet').icon).toBe('fillet')
  })

  it('an edge pick resolves the click to the nearest OTHER entity and carries the click; nothing within reach writes nothing', () => {
    for (const op of ['trim', 'extend', 'fillet', 'chamfer']) {
      expect(PICK_SEQUENCES[op].map((s) => s.kind)).toEqual(['edge', 'point'])
      expect(PICK_SEQUENCES[op][0].keys).toEqual(['edge', 'ex', 'ey'])
    }
    let state = startPicking('trim')
    // (5.05, 3) is 0.05 from the vertical, well within a 0.2 aperture; the selection (H) is skipped.
    const miss = applyPick(state, 5.05, 3, {}, { entities: [H, V], tol: 0.01, exceptId: '7' })
    expect(miss.writes).toEqual([])
    expect(miss.state).toBe(state)
    const hit = applyPick(state, 5.05, 3, {}, { entities: [H, V], tol: 0.2, exceptId: '7' })
    expect(hit.writes).toEqual([['edge', '9'], ['ex', '5.05'], ['ey', '3']])
    state = hit.state
    // No context at all (a typed prompt) writes nothing either.
    expect(applyPick(startPicking('trim'), 5, 3, {}).writes).toEqual([])
    // The second step is a plain point; no ghost on the way.
    expect(ghostFor(state, 8, 1)).toBeNull()
    expect(applyPick(state, 8, 0.5, {}).writes).toEqual([['x', '8'], ['y', '0.5']])
  })

  it('the prompts use the reference words, with the edge as a text field', () => {
    expect(PROMPTS.trim.verb).toBe('TRIM')
    expect(PROMPTS.trim.steps.map((s) => s.ask)).toEqual(['Select cutting edge:', 'Select object to trim (point on the part to remove):'])
    expect(PROMPTS.trim.steps[0].fields).toEqual([['edge', 'edge', 'edge']])
    expect(PROMPTS.extend.steps[0].ask).toBe('Select boundary edge:')
    expect(PROMPTS.fillet.steps.map((s) => s.ask)).toEqual(['Enter fillet radius:', 'Select second object:', 'Point on the first object, on the part to keep:'])
    expect(PROMPTS.fillet.steps[1].fields).toEqual([['edge', 'edge', 'edge'], ['ex', 'edge x'], ['ey', 'edge y']])
    expect(PROMPTS.chamfer.steps.map((s) => s.ask)).toEqual([
      'Enter first chamfer distance:', 'Enter second chamfer distance:', 'Select second line:', 'Point on the first line, on the part to keep:',
    ])
  })
})

// ---- the ribbon: one batch posted, one reply under the verb's name ---------------

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}
function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}
let context = null
let workers = []
function Probe() { context = useEngineSessionContext(); return null }
function mount() {
  workers = []
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
      </DraftingRibbon>
      <CadEditSurface enabled />
    </EngineSessionProvider>,
  )
}
async function openAndLoad(entities, selectedId) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf()] } })
    await Promise.resolve(); await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBe(1))
  workers[0].emit({ type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [] })
  act(() => { context.session.actions.select(selectedId) })
}
const tool = (label) => screen.getByRole('button', { name: label })
const field = (label) => screen.getByLabelText(`ribbon ${label}`, { exact: true })
const lastPost = () => workers[0].posted.filter((m) => m.type === 'applyEdit').pop()

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:verbs')
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup(); context = null })

describe('W4g-6 ribbon: a fillet posts ONE batch and its reply selects the arc under the verb name', () => {
  it('arms on the tool, holds Run while the edge is empty, posts the lowered batch, and reads the reply back as fillet', async () => {
    const Y = { ...V, id: '9', handle: '9', vertices: [[10, 0, 0], [10, 10, 0]] }
    mount()
    await openAndLoad([H, Y], '7')
    fireEvent.click(tool('fillet'))
    await waitFor(() => expect(screen.getByTestId('cockpit-prompt').getAttribute('data-op')).toBe('fillet'))
    fireEvent.change(field('radius'), { target: { value: '2' } })
    // The edge field is the step still waiting: Run holds with its ask as its
    // name, no sentence on the prompt.
    const held = screen.getByRole('button', { name: /^Run \(unavailable: Select second object:\)$/ })
    expect(held.disabled).toBe(true)
    expect(screen.queryByTestId('cockpit-prompt-note')).toBeNull()
    fireEvent.change(field('edge'), { target: { value: '9' } })
    fireEvent.change(field('edge x'), { target: { value: '10' } })
    fireEvent.change(field('edge y'), { target: { value: '8' } })
    fireEvent.change(field('x'), { target: { value: '2' } })
    fireEvent.change(field('y'), { target: { value: '0' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run' }).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))
    const post = lastPost()
    expect(post.op).toBe('batch')
    expect(post.payload.verb).toBe('fillet')
    expect(post.payload.steps).toEqual([
      { op: 'setVertices', payload: { entityId: '7', points: [0, 0, 8, 0], closed: false } },
      { op: 'setVertices', payload: { entityId: '9', points: [10, 2, 10, 10], closed: false } },
      { op: 'createArc', payload: { cx: 8, cy: 2, radius: 2, startDeg: 270, endDeg: 0, layer: 'A' } },
    ])
    expect(context.session.busy).toBe(true)
    const cutH = { ...H, vertices: [[0, 0, 0], [8, 0, 0]] }
    const cutY = { ...Y, vertices: [[10, 2, 0], [10, 10, 0]] }
    workers[0].emit({
      type: 'editApplied', op: 'batch', ok: true, entities: [cutH, cutY, ARC], entityCount: 3,
      createdId: '11', createdIds: ['11'], bytes: new Uint8Array([48, 10]), byteLength: 2,
    })
    expect(context.session.busy).toBe(false)
    expect(context.session.selectedId).toBe('11')
    expect(context.session.status).toMatch(/^fillet applied: entity 11 drawn\./)
    expect(context.session.undoDepth).toBe(1)
  })

  it('a refused batch reads back under the verb too, with the worker step named, and the document stays', async () => {
    mount()
    await openAndLoad([H, V], '7')
    fireEvent.click(tool('trim'))
    await waitFor(() => expect(screen.getByTestId('cockpit-prompt').getAttribute('data-op')).toBe('trim'))
    fireEvent.change(field('edge'), { target: { value: '9' } })
    fireEvent.change(field('x'), { target: { value: '8' } })
    fireEvent.change(field('y'), { target: { value: '0' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run' }).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))
    expect(lastPost()).toMatchObject({ op: 'batch', payload: { verb: 'trim', steps: [{ op: 'setVertices', payload: { entityId: '7', points: [0, 0, 5, 0], closed: false } }] } })
    workers[0].emit({ type: 'editApplied', op: 'batch', ok: false, reason: 'step_0_setVertices:line_zero_length' })
    expect(context.session.busy).toBe(false)
    expect(context.session.status).toBe('Edit refused (trim): step_0_setVertices:line_zero_length')
    expect(context.session.entities).toHaveLength(2)
    expect(context.session.selectedId).toBe('7')
  })

  it('a geometry refusal never reaches the worker: the sentence lands in the status', async () => {
    mount()
    await openAndLoad([H, V], '7')
    fireEvent.click(tool('trim'))
    await waitFor(() => expect(screen.getByTestId('cockpit-prompt').getAttribute('data-op')).toBe('trim'))
    fireEvent.change(field('edge'), { target: { value: '9' } })
    // The pick ON the crossing: valid operands, impossible geometry.
    fireEvent.change(field('x'), { target: { value: '5' } })
    fireEvent.change(field('y'), { target: { value: '0' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run' }).disabled).toBe(false))
    const before = workers[0].posted.length
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))
    expect(workers[0].posted.length).toBe(before)
    expect(context.session.status).toBe('Trim refused: click on the part to remove, away from the crossing.')
    expect(context.session.busy).toBe(false)
  })
})
