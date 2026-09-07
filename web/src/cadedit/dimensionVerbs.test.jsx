// W4g-7b-04c-1: LINEAR / ALIGNED dimension creation, the store builder and
// the diff lowering (Change A's crate is covered natively in lib.rs; Change
// C's mapper/prompts/picks/registry/dock/e2e are record 04c-2).
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import useEngineSession, { buildCreatePayload } from './engineSession.js'
import { diffPlan } from './mutationDiff.js'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'

const STANDARD = Object.freeze(['Standard'])

afterEach(cleanup)

// W4g-7b-04c-3: the same scripted-transport double engineSession.test.jsx
// uses, kept minimal here — this file only needs one store-level row through
// create('dimAligned', ...), the way the ribbon's run() calls it.
class ScriptedWorker {
  constructor() {
    this.posted = []
    this.listeners = { message: [], error: [], messageerror: [] }
  }

  addEventListener(type, cb) {
    if (this.listeners[type]) this.listeners[type].push(cb)
  }

  removeEventListener() {}

  postMessage(data) { this.posted.push(data) }

  terminate() {}

  emit(data) {
    act(() => { this.listeners.message.forEach((cb) => cb({ data })) })
  }
}

function mountSession() {
  const workers = []
  const createWorker = vi.fn(() => {
    const worker = new ScriptedWorker()
    workers.push(worker)
    return worker
  })
  const handle = { current: null, workers }
  function Host() {
    handle.current = useEngineSession({ createWorker })
    return null
  }
  render(<Host />)
  return handle
}

function fileOf(name = 'one.dxf', text = '0\nEOF\n') {
  const bytes = new TextEncoder().encode(text)
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

describe('buildCreatePayload(createDimension): the case table', () => {
  it('LINEAR at rot 0: the exact payload, style defaulting to Standard', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'LINEAR', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', rot: '0', layer: '',
    }, [], STANDARD).payload).toEqual({
      dimtype: 'LINEAR', x1: 0, y1: 0, x2: 3, y2: 4, dx: 1.5, dy: 6, rotationDeg: 0, style: 'Standard', layer: '',
    })
  })

  it('LINEAR at rot 90: the rotation rides through', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'LINEAR', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', rot: '90', layer: '',
    }, [], STANDARD).payload).toMatchObject({ dimtype: 'LINEAR', rotationDeg: 90 })
  })

  it('ALIGNED: the exact payload, rotationDeg always 0', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'ALIGNED', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', layer: '',
    }, [], STANDARD).payload).toEqual({
      dimtype: 'ALIGNED', x1: 0, y1: 0, x2: 3, y2: 4, dx: 1.5, dy: 6, rotationDeg: 0, style: 'Standard', layer: '',
    })
  })

  it('LINEAR rot 450 typed normalizes to 90 before the crate', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'LINEAR', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', rot: '450', layer: '',
    }, [], STANDARD).payload).toMatchObject({ rotationDeg: 90 })
  })

  it('ALIGNED with rot 30 typed refuses: a rotation applies to a linear dimension only', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'ALIGNED', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', rot: '30', layer: '',
    }, [], STANDARD).refusal).toBe('Dimension refused: a rotation applies to a linear dimension only')
  })

  it('coincident definition points refuse', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'LINEAR', x: '1', y: '1', x2: '1', y2: '1', dx: '1.5', dy: '6', layer: '',
    }, [], STANDARD).refusal).toBe('Dimension refused: the two definition points coincide')
  })

  it('a style absent from the catalogue refuses, naming the typed spelling', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'LINEAR', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', style: 'fancy', layer: '',
    }, [], STANDARD).refusal).toBe('Dimension refused: dimension style fancy is not loaded in this drawing')
  })

  it('the catalogue spelling wins over a typed case', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'linear', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', style: 'standard', layer: '',
    }, [], STANDARD).payload).toMatchObject({ dimtype: 'LINEAR', style: 'Standard' })
  })

  it('refuses a malformed dimtype and non-numeric operands', () => {
    expect(buildCreatePayload('createDimension', { dimtype: 'RADIUS', x: '0', y: '0', x2: '3', y2: '4', dx: '1', dy: '1' }, [], STANDARD).refusal)
      .toBe('Dimension refused: choose LINEAR or ALIGNED.')
    expect(buildCreatePayload('createDimension', { dimtype: 'LINEAR', x: 'x', y: '0', x2: '3', y2: '4', dx: '1', dy: '1' }, [], STANDARD).refusal)
      .toBe('Dimension refused: the two definition points must both be numbers.')
    expect(buildCreatePayload('createDimension', { dimtype: 'LINEAR', x: '0', y: '0', x2: '3', y2: '4', dx: 'x', dy: '1' }, [], STANDARD).refusal)
      .toBe('Dimension refused: the dimension line point must be a number.')
  })
})

describe('W4g-7b-04c-3 F1: the seat ops dimLinear / dimAligned reach the store\'s createDimension', () => {
  it('buildCreatePayload(dimAligned): the ALIGNED payload, a typed dimtype input ignored for the seat op', () => {
    expect(buildCreatePayload('dimAligned', {
      dimtype: 'LINEAR', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', layer: '',
    }, [], STANDARD).payload).toEqual({
      dimtype: 'ALIGNED', x1: 0, y1: 0, x2: 3, y2: 4, dx: 1.5, dy: 6, rotationDeg: 0, style: 'Standard', layer: '',
    })
  })

  it('buildCreatePayload(dimLinear) at rot 450 normalizes to 90 before the crate', () => {
    expect(buildCreatePayload('dimLinear', {
      x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', rot: '450', layer: '',
    }, [], STANDARD).payload).toMatchObject({ dimtype: 'LINEAR', rotationDeg: 90 })
  })

  it('a store-level row: create(\'dimAligned\', inputs) posts ONE applyEdit, op createDimension, dimtype ALIGNED — never the seat id', async () => {
    const session = mountSession()
    await act(async () => { await session.current.actions.open(fileOf()) })
    session.workers[0].emit({
      type: 'documentLoaded', documentId: 'one.dxf', entities: [], entityCount: 0, unsupported: [], dimstyles: ['Standard'],
    })
    act(() => session.current.actions.create('dimAligned', { x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6' }))
    const posted = session.workers[0].posted.filter((m) => m.type === 'applyEdit')
    expect(posted).toHaveLength(1)
    expect(posted[0]).toEqual({
      type: 'applyEdit', op: 'createDimension',
      payload: { dimtype: 'ALIGNED', x1: 0, y1: 0, x2: 3, y2: 4, dx: 1.5, dy: 6, rotationDeg: 0, style: 'Standard', layer: '' },
    })
  })
})

describe('W4g-7b-04c-3 F3a: a LINEAR whose rotation projects the definition points to nothing refuses', () => {
  it('(0,0)-(3,0) at rotation 90 projects to nothing; (0,0)-(3,4) at 90 still creates', () => {
    expect(buildCreatePayload('createDimension', {
      dimtype: 'LINEAR', x: '0', y: '0', x2: '3', y2: '0', dx: '1.5', dy: '6', rot: '90', layer: '',
    }, [], STANDARD).refusal).toBe('Dimension refused: the definition points project to nothing along that rotation')
    expect(buildCreatePayload('createDimension', {
      dimtype: 'LINEAR', x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6', rot: '90', layer: '',
    }, [], STANDARD).payload).toMatchObject({ rotationDeg: 90 })
  })
})

describe('W4g-7b-04c-8: dimension style names obey the plan contract before the engine', () => {
  const points = { x: '0', y: '0', x2: '3', y2: '4', dx: '1.5', dy: '6' }

  it.each(['Standard', 'ISO-25'])('%s is loaded and admitted by the builder', (style) => {
    expect(buildCreatePayload('dimLinear', { ...points, style }, [], [style]).payload)
      .toMatchObject({ dimtype: 'LINEAR', style })
  })

  it('the catalogue offers Стандарт in the select, but both the prompt and store refuse it before applyEdit', async () => {
    const worker = new ScriptedWorker()
    const handle = {}
    function Probe() { handle.context = useEngineSessionContext(); return null }
    render(
      <EngineSessionProvider createWorker={() => worker}>
        <Probe />
        <EngineRibbonClusters />
      </EngineSessionProvider>,
    )
    await act(async () => { await handle.context.session.actions.open(fileOf()) })
    worker.emit({ type: 'documentLoaded', documentId: 'one.dxf', entities: [], entityCount: 0, unsupported: [], dimstyles: ['Standard', 'ISO-25', 'Стандарт'] })
    act(() => {
      handle.context.setArmed({ group: 'draw', op: 'dimLinear' })
      for (const [key, value] of Object.entries(points)) handle.context.setInput(key, value)
    })
    const select = screen.getByRole('combobox', { name: 'ribbon style' })
    expect([...select.options].map((option) => option.value)).toContain('Стандарт')
    fireEvent.change(select, { target: { value: 'Стандарт' } })
    const refusal = 'Dimension refused: dimension style Стандарт carries characters the plan contract does not admit'
    expect(buildCreatePayload('dimLinear', { ...points, style: 'Стандарт' }, [], ['Стандарт'])).toEqual({ refusal })
    expect(screen.getByTestId('cockpit-prompt-note').textContent).toBe(refusal)
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    act(() => { handle.context.session.actions.create('dimLinear', handle.context.inputs) })
    expect(handle.context.session.status).toBe(refusal)
    expect(worker.posted.filter((message) => message.type === 'applyEdit')).toHaveLength(0)
    for (const style of ['Standard', 'ISO-25']) {
      fireEvent.change(select, { target: { value: style } })
      expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
    }
  })

  it('the DIMALIGNED seat still ignores rot while the explicit ALIGNED builder refuses it', () => {
    expect(buildCreatePayload('dimAligned', { ...points, rot: '15' }, [], STANDARD).payload)
      .toMatchObject({ dimtype: 'ALIGNED', rotationDeg: 0 })
    expect(buildCreatePayload('createDimension', { ...points, dimtype: 'ALIGNED', rot: '15' }, [], STANDARD).refusal)
      .toBe('Dimension refused: a rotation applies to a linear dimension only')
  })
})

describe('mutationDiff: the LINEAR/ALIGNED add, undo, and the moved refusal', () => {
  const linear = (id, extra = {}) => ({
    id: String(id), handle: String(id), type: 'DIMENSION', dimtype: 'LINEAR', layer: '0', editable: false,
    def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], rotationDeg: 0, style: 'Standard', measurement: 3, ...extra,
  })
  const aligned = (id, extra = {}) => ({
    id: String(id), handle: String(id), type: 'DIMENSION', dimtype: 'ALIGNED', layer: '0', editable: false,
    def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], rotationDeg: 0, style: 'Standard', measurement: 5, ...extra,
  })

  it('a created LINEAR lowers to the 04s add, no measurement', () => {
    expect(diffPlan([], [linear(1280)]).mutations).toEqual({
      added: [{ handle: '500', kind: 'DIMENSION', dimtype: 'LINEAR', def1: [0, 0, 0], def2: [3, 4, 0], dimline: [1.5, 6, 0], rotation: 0, style: 'Standard', layer: '0' }],
    })
  })

  it('a created ALIGNED lowers to the 04s add, no rotation field, no measurement', () => {
    expect(diffPlan([], [aligned(1280)]).mutations).toEqual({
      added: [{ handle: '500', kind: 'DIMENSION', dimtype: 'ALIGNED', def1: [0, 0, 0], def2: [3, 4, 0], dimline: [1.5, 6, 0], style: 'Standard', layer: '0' }],
    })
  })

  it('undo back to the committed list is an empty plan', () => {
    const before = [linear(1280), aligned(1281)]
    expect(diffPlan(before, before)).toEqual({ mutations: {}, count: 0, reason: null })
  })

  it('a removed dimension is a plain removal', () => {
    expect(diffPlan([linear(1280)], [])).toEqual({ mutations: { removed: ['500'] }, count: 1, reason: null })
  })

  it('a moved (or otherwise in-place changed) dimension refuses, never a silent drop', () => {
    const moved = diffPlan([linear(1280)], [linear(1280, { dimline: [9, 9] })])
    expect(moved.mutations).toBeNull()
    expect(moved.reason).toBe('entity 500 is a DIMENSION the plan cannot carry, and it changed')
    const restyled = diffPlan([linear(1280)], [linear(1280, { style: 'Other' })])
    expect(restyled.reason).toBe('entity 500 is a DIMENSION the plan cannot carry, and it changed')
  })
})
