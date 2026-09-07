// W4g-7b-03c: colour, linetype and lineweight, end to end in the browser
// (never the crate): the three builders, MATCHPROP's batch, the diff's
// lowering and the Properties panel's census. Pure rows, no engine.
import { describe, expect, it } from 'vitest'

import { forGroup } from '../lib/actionRegistry.js'
import { parseDrawingCommand } from '../lib/commandWords.js'
import { buildEditPayload, planMatchprop } from './engineSession.js'
import { diffPlan } from './mutationDiff.js'

const CATALOGUE = ['ByLayer', 'ByBlock', 'Continuous', 'HIDDEN', 'DASHED']

describe('the three property builders (buildEditPayload)', () => {
  it('parses colour: ByLayer, ByBlock, a standard name, a bare index, and refuses out of range or unknown text', () => {
    expect(buildEditPayload('setColor', 's1', { aci: 'ByLayer' }).payload.aci).toBe(256)
    expect(buildEditPayload('setColor', 's1', { aci: 'ByBlock' }).payload.aci).toBe(0)
    expect(buildEditPayload('setColor', 's1', { aci: 'red' }).payload.aci).toBe(1)
    expect(buildEditPayload('setColor', 's1', { aci: 7 }).payload.aci).toBe(7)
    expect(buildEditPayload('setColor', 's1', { aci: 257 }).refusal).toBe('Property refused: a colour index is 1 to 255, ByLayer or ByBlock')
    expect(buildEditPayload('setColor', 's1', { aci: 'x' }).refusal).toBe('Property refused: a colour index is 1 to 255, ByLayer or ByBlock')
  })

  it('parses linetype by the catalogue, case-insensitively, storing the catalogue spelling; refuses one absent from it', () => {
    expect(buildEditPayload('setLinetype', 's1', { linetype: 'hidden' }, CATALOGUE).payload.linetype).toBe('HIDDEN')
    expect(buildEditPayload('setLinetype', 's1', { linetype: 'HIDDEN' }, CATALOGUE).payload.linetype).toBe('HIDDEN')
    expect(buildEditPayload('setLinetype', 's1', { linetype: 'Nope' }, CATALOGUE).refusal)
      .toBe('Property refused: linetype Nope is not loaded in this drawing')
  })

  it('parses lineweight: a standard mm value, ByLayer/Default, and refuses an off-grid one', () => {
    expect(buildEditPayload('setLineweight', 's1', { lineweight: 25 }).payload.lineweight).toBe(25)
    expect(buildEditPayload('setLineweight', 's1', { lineweight: 26 }).refusal)
      .toBe('Property refused: lineweight must be a standard value in millimetres, ByLayer, ByBlock or Default')
    expect(buildEditPayload('setLineweight', 's1', { lineweight: 'ByLayer' }).payload.lineweight).toBe(-1)
    expect(buildEditPayload('setLineweight', 's1', { lineweight: 'Default' }).payload.lineweight).toBe(-3)
  })
})

describe('planMatchprop: one batch of the layer plus only the properties that differ', () => {
  const source = { id: 's1', layer: 'A', aci: 1, linetype: 'HIDDEN', lineweight: 25, editable: true }

  it('copies the layer and all three properties as one batch when every one differs', () => {
    const dest = { id: 'd1', layer: 'B', aci: 256, linetype: 'ByLayer', lineweight: -1, editable: true }
    const session = { entities: [source, dest], selectedId: 's1' }
    expect(planMatchprop(session, { edge: 'd1' })).toEqual({
      steps: [
        { op: 'setLayer', entityId: 'd1', layer: 'A' },
        { op: 'setColor', entityId: 'd1', aci: 1 },
        { op: 'setLinetype', entityId: 'd1', linetype: 'HIDDEN' },
        { op: 'setLineweight', entityId: 'd1', lineweight: 25 },
      ],
    })
  })

  it('carries only the properties that actually differ', () => {
    const dest = { id: 'd2', layer: 'A', aci: 1, linetype: 'ByLayer', lineweight: 25, editable: true }
    const session = { entities: [source, dest], selectedId: 's1' }
    expect(planMatchprop(session, { edge: 'd2' })).toEqual({
      steps: [{ op: 'setLinetype', entityId: 'd2', linetype: 'HIDDEN' }],
    })
  })

  it('refuses a destination that is the selection itself', () => {
    const session = { entities: [source], selectedId: 's1' }
    expect(planMatchprop(session, { edge: 's1' }))
      .toEqual({ refusal: 'Match refused: the destination must be a different entity from the selection.' })
  })
})

describe('the diff: colour-only changes, styled adds and true colour', () => {
  const line = (id, extra = {}) => ({
    id: String(id), type: 'LINE', layer: '0', closed: false,
    vertices: [[0, 0, 0], [3, 4, 0]], radius: null, startDeg: null, endDeg: null, ...extra,
  })

  it('a colour-only change on a retained LINE lowers to set_color alone', () => {
    const before = [line(10, { aci: 256 })]
    const after = [line(10, { aci: 1 })]
    expect(diffPlan(before, after)).toEqual({ mutations: { set_color: [{ handle: 'A', aci: 1 }] }, count: 1, reason: null })
  })

  it('a created LINE coloured 1 is a styled add, never a set_color', () => {
    const after = [line(10, { aci: 1 })]
    expect(diffPlan([], after)).toEqual({
      mutations: { added: [{ handle: 'A', kind: 'LINE', layer: '0', pts: [[0, 0, 0], [3, 4, 0]], color: 1 }] },
      count: 1,
      reason: null,
    })
  })

  it('a true colour set in the browser is refused with its sentence', () => {
    const before = [line(10)]
    const after = [line(10, { trueColor: [1, 2, 3] })]
    expect(diffPlan(before, after)).toEqual({
      mutations: null, count: 0, reason: 'entity A has a true colour the plan cannot carry',
    })
  })

  it('an untouched true-coloured entity is carried as-is, no set', () => {
    const before = [line(10, { trueColor: [1, 2, 3] })]
    const after = [line(10, { trueColor: [1, 2, 3] })]
    expect(diffPlan(before, after)).toEqual({ mutations: {}, count: 0, reason: null })
  })
})

describe('the typed words', () => {
  it('COLOR/COL, LINETYPE/LT and LWEIGHT/LW all arm their property op', () => {
    expect(parseDrawingCommand('color')).toMatchObject({ op: 'setColor', verb: 'COLOR' })
    expect(parseDrawingCommand('col')).toMatchObject({ op: 'setColor', verb: 'COLOR' })
    expect(parseDrawingCommand('linetype')).toMatchObject({ op: 'setLinetype', verb: 'LINETYPE' })
    expect(parseDrawingCommand('lt')).toMatchObject({ op: 'setLinetype', verb: 'LINETYPE' })
    expect(parseDrawingCommand('lweight')).toMatchObject({ op: 'setLineweight', verb: 'LWEIGHT' })
    expect(parseDrawingCommand('lw')).toMatchObject({ op: 'setLineweight', verb: 'LWEIGHT' })
  })
})

describe('the Properties panel census', () => {
  it('seats Match and the three property combos, in registry order', () => {
    expect(forGroup('modify').filter((a) => a.panel === 'properties').map((a) => a.op))
      .toEqual(['matchprop', 'setColor', 'setLinetype', 'setLineweight'])
  })
})
