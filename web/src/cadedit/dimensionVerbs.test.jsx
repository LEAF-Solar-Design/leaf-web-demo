// W4g-7b-04c-1: LINEAR / ALIGNED dimension creation, the store builder and
// the diff lowering (Change A's crate is covered natively in lib.rs; Change
// C's mapper/prompts/picks/registry/dock/e2e are record 04c-2).
import { describe, expect, it } from 'vitest'
import { buildCreatePayload } from './engineSession.js'
import { diffPlan } from './mutationDiff.js'

const STANDARD = Object.freeze(['Standard'])

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
